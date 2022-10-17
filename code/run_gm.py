import os, sys
sys.path.insert(0,'.')
sys.path.insert(0,'./src')
sys.path.insert(0,'./src/mae')
sys.path.insert(0,'./src/ldm')
import numpy as np
import torch
from torch.utils.data import DataLoader
import torch.nn as nn
from config import Config_Generative_Model
from src.dataset import create_Kamitani_dataset, fmri_latent_dataset, create_Shen2019_dataset, create_BOLD5000_dataset
from src.mae.mae_for_fmri import fmri_encoder
from src.ldm.ldm_for_fmri import fLDM
import argparse
import datetime
import wandb
import torchvision.transforms as transforms
from einops import rearrange
from PIL import Image
import pytorch_lightning as pl
from pytorch_lightning.loggers import WandbLogger
from pytorch_lightning.callbacks import StochasticWeightAveraging
from src.eval_metrics import get_similarity_metric
import copy

def to_image(img):
    if img.shape[-1] != 3:
        img = rearrange(img, 'c h w -> h w c')
    img = 255. * img
    return Image.fromarray(img.astype(np.uint8))

def channel_last(img):
        if img.shape[-1] == 3:
            return img
        return rearrange(img, 'c h w -> h w c')

def create_fmri_latents_from_dataset(dataset):
    latents = np.expand_dims(dataset.fmri, axis=1)
    latent_dataset = fmri_latent_dataset(latents, dataset.image, dataset.img_class, dataset.img_class_name,
            dataset.naive_label, dataset.fmri_transform, dataset.image_transform, dataset.num_per_sub)
    return latent_dataset

def get_eval_metric(samples, avg=True):
    metric_list = ['mse', 'pcc', 'ssim', 'psm']
    res_list = []
    
    gt_images = [img[0] for img in samples]
    gt_images = rearrange(np.stack(gt_images), 'n c h w -> n h w c')
    samples_to_run = np.arange(1, len(samples[0])) if avg else [1]
    for m in metric_list:
        res_part = []
        for s in samples_to_run:
            pred_images = [img[s] for img in samples]
            pred_images = rearrange(np.stack(pred_images), 'n c h w -> n h w c')
            res = get_similarity_metric(pred_images, gt_images, method='pair-wise', metric_name=m)
            res_part.append(np.mean(res))
        res_list.append(np.mean(res_part))     
    res_part = []
    for s in samples_to_run:
        pred_images = [img[s] for img in samples]
        pred_images = rearrange(np.stack(pred_images), 'n c h w -> n h w c')
        res = get_similarity_metric(pred_images, gt_images, 'class', None, 
                        n_way=50, num_trials=50, top_k=1, device='cuda')
        res_part.append(np.mean(res))
    res_list.append(np.mean(res_part))
    metric_list.append('top-1-class')

    return res_list, metric_list
               
def generate_images(generative_model, fmri_latents_dataset_train, fmri_latents_dataset_test, config):
    grid, _ = generative_model.generate(fmri_latents_dataset_train, config.num_samples, 
                config.ddim_steps, config.HW, 10) # generate 10 instances
    grid_imgs = Image.fromarray(grid.astype(np.uint8))
    grid_imgs.save(os.path.join(config.output_path, 'samples_train.png'))
    wandb.log({'summary/samples_train': wandb.Image(grid_imgs)})
    if config.dataset == 'Kamitani_2017':
        subs = config.kam_subs
    elif config.dataset == 'Shen_2019':
        subs = config.shen_subs
    else:
        raise NotImplementedError

    for sub in subs:
        fmri_latents_dataset_test.switch_sub_view(sub, subs)
        grid, samples = generative_model.generate(fmri_latents_dataset_test, config.num_samples, 
                    config.ddim_steps, config.HW)
        grid_imgs = Image.fromarray(grid.astype(np.uint8))
        grid_imgs.save(os.path.join(config.output_path,f'./samples_test_{sub}.png'))
        for sp_idx, imgs in enumerate(samples):
            for copy_idx, img in enumerate(imgs[1:]):
                img = rearrange(img, 'c h w -> h w c')
                Image.fromarray(img).save(os.path.join(config.output_path, 
                                f'./test{sp_idx}-{copy_idx}_{sub}.png'))

        wandb.log({f'summary/samples_test_{sub}': wandb.Image(grid_imgs)})

        metric, metric_list = get_eval_metric(samples, avg=config.eval_avg)
        metric_dict = {f'summary/pair-wise_{k}_{sub}':v for k, v in zip(metric_list[:-1], metric[:-1])}
        metric_dict[f'summary/{metric_list[-1]}_{sub}'] = metric[-1]
        wandb.log(metric_dict)

        # metric, metric_list = get_eval_metric(samples, method='n-way', n=2, n_trials=100)
        # metric_dict = {f'summary/2way_{k}_{sub}':v for k, v in zip(metric_list, metric)}
        # wandb.log(metric_dict)

def normalize(img):
    if img.shape[-1] == 3:
        img = rearrange(img, 'h w c -> c h w')
    img = torch.tensor(img)
    img = img * 2.0 - 1.0 # to -1 ~ 1
    return img

class random_crop:
    def __init__(self, size, p):
        self.size = size
        self.p = p
    def __call__(self, img):
        if torch.rand(1) < self.p:
            return transforms.RandomCrop(size=(self.size, self.size))(img)
        return img

def fmri_transform(x, sparse_rate=0.2):
    # x: 1, num_voxels
    x_aug = copy.deepcopy(x)
    idx = np.random.choice(x.shape[0], int(x.shape[0]*sparse_rate), replace=False)
    x_aug[idx] = 0
    return torch.FloatTensor(x_aug)

def main(config):
    # project setup
    device = torch.device(f'cuda:{config.local_rank}') if torch.cuda.is_available() else torch.device('cpu')
    torch.manual_seed(config.seed)
    np.random.seed(config.seed)

    # prepare dataset
    # normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
    #                                 std=[0.229, 0.224, 0.225])

    crop_pix = int(config.crop_ratio*config.img_size)
    img_transform_train = transforms.Compose([
        normalize,
        random_crop(config.img_size-crop_pix, p=0.5),
        transforms.Resize((256, 256)), 
        channel_last
    ])
    img_transform_test = transforms.Compose([
        normalize, transforms.Resize((256, 256)), 
        channel_last
    ])
    if config.dataset == 'Kamitani_2017':
        kam_dataset_train, kam_dataset_test = create_Kamitani_dataset(config.kam_path, config.roi, config.patch_size, 
                fmri_transform=fmri_transform, image_transform=[img_transform_train, img_transform_test], 
                subjects=config.kam_subs, test_category=config.test_category)
        fmri_latents_dataset_train = create_fmri_latents_from_dataset(kam_dataset_train)
        fmri_latents_dataset_test = create_fmri_latents_from_dataset(kam_dataset_test)
        num_voxels = kam_dataset_train.num_voxels
    elif config.dataset == 'Shen_2019':
        fmri_latents_dataset_train, fmri_latents_dataset_test = create_Shen2019_dataset(config.shen_path, config.kam_path, config.roi, config.patch_size, 
                fmri_transform=fmri_transform, image_transform=[img_transform_train, img_transform_test], 
                subjects=config.shen_subs)
        num_voxels = fmri_latents_dataset_train.num_voxels

    elif config.dataset == 'BOLD5000':
        fmri_latents_dataset_train, fmri_latents_dataset_test = create_BOLD5000_dataset(config.bold5000_path, config.patch_size, 
                fmri_transform=fmri_transform, image_transform=[img_transform_train, img_transform_test], 
                subjects=config.bold5000_subs)
        num_voxels = fmri_latents_dataset_train.num_voxels
    else:
        raise NotImplementedError

    # prepare pretrained mae 
    pretrain_mae_metafile = torch.load(config.pretrain_mae_path, map_location='cpu')
    # create generateive model
    generative_model = fLDM(pretrain_mae_metafile, num_voxels,
                device=device, pretrain_root=config.pretrain_gm_path, logger=config.logger, 
                mask_ratio=config.mask_ratio, ddim_steps=config.ddim_steps, 
                global_pool=config.global_pool, use_time_cond=config.use_time_cond)
    
    # resume training if applicable
    if config.checkpoint_path is not None:
        model_meta = torch.load(config.checkpoint_path, map_location='cpu')
        generative_model.model.load_state_dict(model_meta['model_state_dict'])
        print('model resumed')
    # finetune the model
    generative_model.finetune(config.trainer, fmri_latents_dataset_train, fmri_latents_dataset_test,
                config.batch_size1, config.lr1, config.output_path, config.local_rank, config=config)

    # generate images
    # generate limited train images and generate images for subjects seperately
    if config.local_rank == 0:
        generate_images(generative_model, fmri_latents_dataset_train, fmri_latents_dataset_test, config)

    return

def get_args_parser():
    parser = argparse.ArgumentParser('MAE pre-training for fMRI', add_help=False)
    # project parameters
    parser.add_argument('--seed', type=int)
    parser.add_argument('--kam_path', type=str)
    parser.add_argument('--pretrain_mae_path', type=str)
    parser.add_argument('--roi', type=str)
    parser.add_argument('--patch_size', type=int)

    # finetune parameters
    parser.add_argument('--batch_size', type=int)
    parser.add_argument('--lr', type=float)
    parser.add_argument('--num_epoch', type=int)

    # diffusion sampling parameters
    parser.add_argument('--num_samples', type=int)
    parser.add_argument('--ddim_steps', type=int)
    parser.add_argument('--scale', type=float)
    parser.add_argument('--ddim_eta', type=float)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', type=int)

    return parser

def update_config(args, config):
    for attr in config.__dict__:
        if hasattr(args, attr):
            if getattr(args, attr) != None:
                setattr(config, attr, getattr(args, attr))
    return config

def create_readme(config, path):
    print(config.__dict__)
    with open(os.path.join(path, 'README.md'), 'w+') as f:
        print(config.__dict__, file=f)


def wandb_init(config, output_path):
    wandb.init( project="image_generation",
                group=config.group_name,
                entity="jqing", 
                config=config,
                reinit=True)
    create_readme(config, output_path)

def wandb_finish():
    wandb.finish()

def create_trainer(num_epoch, precision=32, accumulate_grad_batches=2,logger=None,check_val_every_n_epoch=0):
    acc = 'gpu' if torch.cuda.is_available() else 'cpu'
    return pl.Trainer(accelerator=acc, max_epochs=num_epoch, logger=logger, 
            precision=precision, accumulate_grad_batches=accumulate_grad_batches,
            enable_checkpointing=False, enable_model_summary=False, gradient_clip_val=0.5,
            check_val_every_n_epoch=check_val_every_n_epoch)
  
if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    config = Config_Generative_Model()
    config = update_config(args, config)
    
    if config.checkpoint_path is not None:
        model_meta = torch.load(config.checkpoint_path, map_location='cpu')
        ckp = config.checkpoint_path
        config = model_meta['config']
        config.checkpoint_path = ckp
        print('Resuming from checkpoint: {}'.format(config.checkpoint_path))

    # config.local_rank = int(os.environ('LOCAL_RANK')) if 'LOCAL_RANK' in os.environ else 0
    output_path = os.path.join(config.root_path, 'results', 'generation',  '%s'%(datetime.datetime.now().strftime("%d-%m-%Y-%H:%M:%S")))
    config.output_path = output_path
    os.makedirs(output_path, exist_ok=True)
    
    if config.local_rank == 0:
        wandb_init(config, output_path)

    logger = WandbLogger()
    config.trainer = [
        create_trainer(config.num_epoch_1, config.precision, config.accumulate_grad, logger, check_val_every_n_epoch=5)
    ]
    config.logger = logger
    main(config)
    if config.local_rank == 0:
        wandb_finish()
    
        




    