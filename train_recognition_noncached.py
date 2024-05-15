import os
import sys
import math
import argparse
import json
from pathlib import Path
import torch
print(torch.__version__)
import torch.backends.cudnn as cudnn
import webdataset as wds
import torchvision.transforms as transforms
from torchvision.datasets import ImageFolder
from torch.utils.data import DataLoader, SequentialSampler

import tae
import util.misc as misc
from util.misc import NativeScalerWithGradNormCount as NativeScaler


def get_args_parser():
    parser = argparse.ArgumentParser('Training on a downstream recognition task', add_help=False)
    parser.add_argument('--batch_size', default=256, type=int, help='Total batch size')
    parser.add_argument('--accum_iter', default=1, type=int, help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')
    parser.add_argument('--save_prefix', default="", type=str, help="""prefix for saving checkpoint and log files""")
    parser.add_argument('--save_freq', default=10000, type=int, help='Save checkpoint every this many iterations.')

    # Model parameters
    parser.add_argument('--model', default='', type=str, help='Name of model to train')
    parser.add_argument('--model_ckpt', default='', type=str, help='Model checkpoint to resume from')
    parser.add_argument('--num_classes', default=None, type=int, help='number of classes')
    parser.add_argument('--input_size', default=224, type=int, help='images input size')

    # Encoder parameters
    parser.add_argument('--encoder', default='', type=str, help='Name of encoder')
    parser.add_argument('--encoder_ckpt', default='', type=str, help='Encoder checkpoint to resume from')

    # Optimizer parameters
    parser.add_argument('--weight_decay', type=float, default=0.05, help='weight decay (default: 0.05)')
    parser.add_argument('--lr', type=float, default=0.0001, help='learning rate (absolute lr)')

    # Dataset parameters
    parser.add_argument('--train_data_path', default='', type=str)
    parser.add_argument('--val_data_path', default='', type=str)
    parser.add_argument('--num_workers', default=16, type=int, help='Number of data loading workers.')

    # Misc
    parser.add_argument('--output_dir', default='./output_dir', help='path where to save, empty for no saving')

    return parser


def main(args):
    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))
    cudnn.benchmark = True

    device_encoder = torch.device('cuda:0')
    device_model = torch.device('cuda:1')

    # validation transforms
    val_transform = transforms.Compose([
        transforms.Resize(args.input_size + 32, interpolation=3),
        transforms.CenterCrop(args.input_size),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])

    # training transforms
    train_transform = transforms.Compose([
        transforms.RandomResizedCrop(args.input_size, scale=[0.2, 1.0], ratio=[3.0/4.0, 4.0/3.0], interpolation=3),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.485, 0.456, 0.406), (0.229, 0.224, 0.225)),
    ])

    # train and val datasets and loaders
    train_dataset = wds.WebDataset(args.train_data_path, resampled=True).shuffle(10000, initial=10000).decode("pil").to_tuple("jpg", "cls").map_tuple(train_transform, lambda x: x)
    train_loader = wds.WebLoader(train_dataset, batch_size=args.batch_size, num_workers=args.num_workers)

    val_dataset = ImageFolder(args.val_data_path, transform=val_transform)
    val_sampler = SequentialSampler(val_dataset)
    val_loader = DataLoader(val_dataset, sampler=val_sampler, batch_size=8*args.batch_size, num_workers=args.num_workers, pin_memory=True, drop_last=False)  # NOTE: we use a larger batch size for eval
    print(f"Train and val data loaded.")

    # define the model
    model = tae.__dict__[args.model](num_classes=args.num_classes)
    model.to(device_model)
    print(f"Model: {model}")
    print(f"Number of params (M): {(sum(p.numel() for p in model.parameters() if p.requires_grad) / 1.e6)}")

    # define the encoder
    encoder = tae.__dict__[args.encoder]()
    encoder.to(device_encoder)
    encoder.eval()
    print(f"Model: {encoder}")
    print(f"Number of params (M): {(sum(p.numel() for p in encoder.parameters() if p.requires_grad) / 1.e6)}")

    # set wd as 0 for bias and norm layers
    param_groups = misc.add_weight_decay(model, args.weight_decay, bias_wd=False)
    optimizer = torch.optim.AdamW(param_groups, lr=args.lr, betas=(0.9, 0.95), fused=True)  # setting fused True for faster updates (hopefully)
    criterion = torch.nn.CrossEntropyLoss()
    loss_scaler = NativeScaler()

    misc.load_model(args.model_ckpt, model, optimizer=optimizer, loss_scaler=loss_scaler)
    misc.load_model(args.encoder_ckpt, encoder)

    model.train()
    metric_logger = misc.MetricLogger(delimiter="  ")
    optimizer.zero_grad()

    best_eval_acc1 = 0.0
    it = 0

    print("Starting TAE training!")
    # infinite stream for iterable webdataset
    for it, (samples, targets) in enumerate(train_loader):

        with torch.no_grad():
            with torch.cuda.amp.autocast():
                samples = samples.to(device_encoder, non_blocking=True)
                samples = encoder.forward_encoder(samples)

        # move to gpu
        samples = samples.to(device_model, non_blocking=True)
        targets = targets.to(device_model, non_blocking=True)

        with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(outputs, targets)
        
        loss_value = loss.item()

        if not math.isfinite(loss_value):
            print("Loss is {}, stopping training".format(loss_value))
            sys.exit(1)

        loss = loss / args.accum_iter
        loss_scaler(loss, optimizer, parameters=model.parameters(), update_grad=(it + 1) % args.accum_iter == 0)
        if (it + 1) % args.accum_iter == 0:
            optimizer.zero_grad()

        torch.cuda.synchronize()

        metric_logger.update(loss=loss_value)

        if it != 0 and it % args.save_freq == 0:
            # estimate eval loss
            print(f"Iteration {it}, evaluating ...")
            test_stats = evaluate(val_loader, model, encoder, device_model, device_encoder)
            
            # save checkpoint only if eval_loss decreases
            if test_stats['acc1'] > best_eval_acc1:
                print("Best eval accuracy improved! Saving checkpoint.")
                save_dict = {
                    'model': model.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'args': args,
                    'iteration': it,
                    'scaler': loss_scaler.state_dict(),
                }

                misc.save_on_master(save_dict, os.path.join(args.output_dir, f"{args.save_prefix}_checkpoint.pth"))
                best_eval_acc1 = test_stats['acc1']

            # gather the stats from all processes
            metric_logger.synchronize_between_processes()
            train_stats = {k: meter.global_avg for k, meter in metric_logger.meters.items()}
            log_stats = {**{f'train_{k}': v for k, v in train_stats.items()}, 'eval_loss': test_stats['loss'], 'iteration': it}

            # write log
            if misc.is_main_process():
                with (Path(args.output_dir) / (args.save_prefix + "_log.txt")).open("a") as f:
                    f.write(json.dumps(log_stats) + "\n")

            # start a fresh logger to wipe off old stats
            metric_logger = misc.MetricLogger(delimiter="  ")

            # switch back to train mode, not 100% sure if this is strictly necessary since we're passing the unwrapped model to eval now
            model.train()

        it += 1    

@torch.no_grad()
def evaluate(val_loader, model, encoder, device_model, device_encoder):

    criterion = torch.nn.CrossEntropyLoss()
    metric_logger = misc.MetricLogger(delimiter="  ")

    # switch model to eval mode
    model.eval()

    for _, (samples, targets) in enumerate(val_loader):

        with torch.cuda.amp.autocast():
            samples = samples.to(device_encoder, non_blocking=True)
            samples = encoder.forward_encoder(samples)

        # move to gpu
        samples = samples.to(device_model, non_blocking=True)
        targets = targets.to(device_model, non_blocking=True)

        with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(outputs, targets)

        # compute loss
        with torch.cuda.amp.autocast():
            outputs = model(samples)
            loss = criterion(outputs, targets)

        acc1, acc5 = misc.accuracy(outputs, targets, topk=(1, 5))

        bsize = samples.shape[0]
        metric_logger.update(loss=loss.item())
        metric_logger.meters['acc1'].update(acc1.item(), n=bsize)
        metric_logger.meters['acc5'].update(acc5.item(), n=bsize)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print('* Acc@1 {top1.global_avg:.3f} Acc@5 {top5.global_avg:.3f} loss {losses.global_avg:.3f}'.format(
        top1=metric_logger.acc1, top5=metric_logger.acc5, losses=metric_logger.loss
        ))

    return {k: meter.global_avg for k, meter in metric_logger.meters.items()}

if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)