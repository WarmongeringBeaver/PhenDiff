import os
from pathlib import Path

import numpy as np
import torch
from accelerate import Accelerator
from accelerate.logging import get_logger
from accelerate.utils import DistributedDataParallelKwargs, ProjectConfiguration
from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    StableDiffusionPipeline,
    UNet2DConditionModel,
)
from diffusers.optimization import get_scheduler
from diffusers.training_utils import EMAModel
from transformers import CLIPTextModel, CLIPTokenizer

import wandb
from src.args_parser import parse_args
from src.utils_dataset import setup_dataset
from src.utils_misc import (
    args_checker,
    create_repo_structure,
    setup_logger,
    setup_xformers_memory_efficient_attention,
)
from src.utils_training import (
    checkpoint_model,
    generate_samples_and_compute_metrics,
    get_training_setup,
    perform_training_epoch,
    resume_from_checkpoint,
)

logger = get_logger(__name__, log_level="INFO")


def main(args):
    # ------------------------- Checks -------------------------
    args_checker(args)

    # ----------------------- Accelerator ----------------------
    accelerator_project_config = ProjectConfiguration(
        total_limit=args.checkpoints_total_limit,
        automatic_checkpoint_naming=False,
        project_dir=args.output_dir,
    )

    kwargs = DistributedDataParallelKwargs(find_unused_parameters=True)

    accelerator = Accelerator(
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        mixed_precision=args.mixed_precision,
        log_with=args.logger,
        project_config=accelerator_project_config,
        kwargs_handlers=[kwargs],
    )

    # -------------------------- WandB -------------------------
    wandb_project_name = args.output_dir.lstrip("experiments/")
    logger.info(f"Logging to project {wandb_project_name}")
    accelerator.init_trackers(
        project_name=wandb_project_name,
        config=args,
    )

    # Make one log on every process with the configuration for debugging.
    setup_logger(logger, accelerator)

    # ------------------- Repository scruture ------------------
    (
        image_generation_tmp_save_folder,
        initial_pipeline_save_folder,
        full_pipeline_save_folder,
        repo,
    ) = create_repo_structure(args, accelerator)

    # ------------------------- Dataset ------------------------
    dataset, nb_classes = setup_dataset(args, logger)

    train_dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.train_batch_size,
        shuffle=True,
        num_workers=args.dataloader_num_workers,
    )

    # ------------------- Pretrained Pipeline ------------------
    # Download the full pretrained pipeline.
    # Note that the actual folder to pull components from is
    # initial_pipeline_save_folder/snapshots/<gibberish>/ (probably a hash?)
    # hence the need to get the *true* save folder (initial_pipeline_save_path)
    initial_pipeline_save_path = StableDiffusionPipeline.download(
        args.pretrained_model_name_or_path,
        cache_dir=initial_pipeline_save_folder,
        # override some useless components
        safety_checker=None,
        feature_extractor=None,
    )

    # Load the pretrained components
    autoencoder_model: AutoencoderKL = AutoencoderKL.from_pretrained(
        initial_pipeline_save_path,
        subfolder="vae",
        local_files_only=True,
    )
    if args.learn_denoiser_from_scratch:
        denoiser_model: UNet2DConditionModel = UNet2DConditionModel.from_config(
            Path(initial_pipeline_save_path, "unet", "config.json"),
        )
    else:
        denoiser_model: UNet2DConditionModel = UNet2DConditionModel.from_pretrained(
            initial_pipeline_save_path,
            subfolder="unet",
            local_files_only=True,
        )
    text_encoder: CLIPTextModel = CLIPTextModel.from_pretrained(
        initial_pipeline_save_path,
        subfolder="text_encoder",
        local_files_only=True,
    )
    tokenizer: CLIPTokenizer = CLIPTokenizer.from_pretrained(
        initial_pipeline_save_path,
        subfolder="tokenizer",
        local_files_only=True,
    )

    # Move components to device
    autoencoder_model.to(accelerator.device)
    denoiser_model.to(accelerator.device)
    text_encoder.to(accelerator.device)

    # ❄️ >>> Freeze components <<< ❄️
    autoencoder_model.requires_grad_(False)
    text_encoder.requires_grad_(False)

    # --------------------- Noise scheduler --------------------
    noise_scheduler = DDIMScheduler(
        num_train_timesteps=args.num_training_steps,
        beta_start=args.beta_start,
        beta_end=args.beta_end,
        beta_schedule=args.beta_schedule,
        prediction_type=args.prediction_type,
    )

    # Create EMA for the unet model
    if args.use_ema:
        ema_unet = EMAModel(
            denoiser_model.parameters(),
            decay=args.ema_max_decay,
            use_ema_warmup=True,
            inv_gamma=args.ema_inv_gamma,
            power=args.ema_power,
            model_cls=UNet2DConditionModel,
            model_config=denoiser_model.config,
        )
    else:
        ema_unet = None

    if args.enable_xformers_memory_efficient_attention:
        setup_xformers_memory_efficient_attention(denoiser_model, logger)

    # track gradients
    if accelerator.is_main_process:
        wandb.watch(denoiser_model)

    # ------------------------ Optimizer -----------------------
    optimizer = torch.optim.AdamW(
        denoiser_model.parameters(),
        lr=args.learning_rate,
        betas=(args.adam_beta1, args.adam_beta2),
        weight_decay=args.adam_weight_decay,
        eps=args.adam_epsilon,
    )

    # ----------------- Learning rate scheduler -----------------
    lr_scheduler = get_scheduler(
        args.lr_scheduler,
        optimizer=optimizer,
        num_warmup_steps=args.lr_warmup_steps * args.gradient_accumulation_steps,
        num_training_steps=(len(train_dataloader) * args.num_epochs),
    )

    # ------------------ Distributed compute  ------------------
    denoiser_model, optimizer, train_dataloader, lr_scheduler = accelerator.prepare(
        denoiser_model, optimizer, train_dataloader, lr_scheduler
    )

    # --------------------- Training setup ---------------------
    if args.use_ema:
        ema_unet.to(accelerator.device)

    # We need to initialize the trackers we use, and also store our configuration.
    # The trackers initializes automatically on the main process.
    if accelerator.is_main_process:
        run = os.path.split(__file__)[-1].split(".")[0]
        accelerator.init_trackers(run)

    first_epoch = 0
    global_step = 0
    resume_step = 0

    (
        num_update_steps_per_epoch,
        tot_nb_eval_batches,
        actual_eval_batch_sizes_for_this_process,
    ) = get_training_setup(args, accelerator, train_dataloader, logger, dataset)

    # ----------------- Resume from checkpoint -----------------
    if args.resume_from_checkpoint:
        first_epoch, resume_step, global_step = resume_from_checkpoint(
            args, logger, accelerator, num_update_steps_per_epoch, global_step
        )

    # ---------------------- Seeds & RNGs ----------------------
    rng = np.random.default_rng()  # TODO: seed this

    # ---------------------- Training loop ---------------------
    for epoch in range(first_epoch, args.num_epochs):
        # Training epoch
        global_step = perform_training_epoch(
            denoiser_model,
            autoencoder_model,
            tokenizer,
            text_encoder,
            num_update_steps_per_epoch,
            accelerator,
            epoch,
            train_dataloader,
            args,
            first_epoch,
            resume_step,
            noise_scheduler,
            global_step,
            optimizer,
            lr_scheduler,
            ema_unet,
            logger,
        )

        # Generate sample images for visual inspection & metrics computation
        if (
            epoch % args.save_images_epochs == 0 or epoch == args.num_epochs - 1
        ) and epoch > 0:
            generate_samples_and_compute_metrics(
                args,
                accelerator,
                denoiser_model,
                ema_unet,
                autoencoder_model,
                text_encoder,
                tokenizer,
                noise_scheduler,
                image_generation_tmp_save_folder,
                actual_eval_batch_sizes_for_this_process,
                epoch,
                global_step,
            )

        if (
            accelerator.is_main_process
            and (epoch % args.save_model_epochs == 0 or epoch == args.num_epochs - 1)
            and epoch != 0
        ):
            checkpoint_model(
                accelerator,
                denoiser_model,
                autoencoder_model,
                text_encoder,
                tokenizer,
                args,
                ema_unet,
                noise_scheduler,
                initial_pipeline_save_path,
                full_pipeline_save_folder,
                repo,
                epoch,
            )

        # do not start new epoch before generation & checkpointing is done
        accelerator.wait_for_everyone()

    accelerator.end_training()


if __name__ == "__main__":
    args = parse_args()
    main(args)
