import os
import sys
import time
import tabulate

from torchcontrib.optim import SWA

import torch
import torch.optim as optim
import torch.nn as nn
import numpy as np
from torch.utils.tensorboard import SummaryWriter
import pickle
import argparse
import random
from utils import (
    save_checkpoint,
    load_data,
    train,
    validate,
    )

from models.cascade_net import CascadeNet
from models.vaereg import VAEReg
from torch.utils.data import DataLoader
from scheduler import PolynomialLR
import losses
from models.models import *
from data_loader import BraTSTrainDataset

#from apex import amp
from apex_dummy import amp

parser = argparse.ArgumentParser(description='Train glioma segmentation model.')
lr_add_cnst = 1e-6
# In this directory is stored the script used to start the training,
# the most recent and best checkpoints, and a directory of logs.
parser.add_argument('--dir', type=str, required=True, metavar='PATH',
    help='The directory to write all output to.')

parser.add_argument('--data_dir', type=str, required=True, metavar='PATH TO DATA',
    help='Path to where the data is located.')

parser.add_argument('--model', type=str, default=None, required=True, metavar='MODEL',
                        help='model class (default: None)')

parser.add_argument('--device', type=int, required=True, metavar='N',
    help='Which device to use for training.')

parser.add_argument('--upsampling', type=str, default='bilinear', 
    choices=['bilinear', 'deconv'], 
    help='upsampling algorithm to use in decoder (default: bilinear)')

parser.add_argument('--loss', type=str, default='avgdice', 
    choices=['dice', 'recon', 'avgdice', 'vae'], 
    help='which loss to use during training (default: avgdice)')

parser.add_argument('-a', '--augment_data', action='store_true', 
    help='augment training data with mirroring, shifts, and scaling (default: off)')

parser.add_argument('--mixed_precision', action='store_true', 
    help='mixed precision flag (default: off)')

parser.add_argument('-b', '--debug', action='store_true', 
    help='use debug mode which only uses a couple examples for training and testing (default: off)')

parser.add_argument('-L', '--large_patch', action='store_true', 
        help='use patch size 160x192x128 (default patch size: 128x128x128)')

parser.add_argument('--cross_val', action='store_true', 
    help='use train/val split of full dataset (default: off)')

parser.add_argument('--swa', action='store_true', 
    help='use stochastic weight averaging during training (default: off)')

parser.add_argument('--seed', type=int, default=1, metavar='S', 
    help='random seed (default: 1)')

parser.add_argument('--wd', type=float, default=1e-4, 
    help='weight decay (default: 1e-4)')

parser.add_argument('--resume', type=str, default=None, metavar='PATH',
                        help='checkpoint to resume training from (default: None)')

parser.add_argument('--pretrain', type=str, default=None, metavar='PATH',
                        help='pretrained model to start training from (default: None)')

parser.add_argument('--epochs', type=int, default=300, metavar='N', 
    help='number of epochs to train (default: 300)')

parser.add_argument('--num_workers', type=int, default=4, metavar='N', 
    help='number of workers to assign to dataloader (default: 4)')

parser.add_argument('--batch_size', type=int, default=1, metavar='N', 
    help='batch_size (default: 1)')

parser.add_argument('--save_freq', type=int, default=25, metavar='N', 
    help='save frequency (default: 25)')

parser.add_argument('--eval_freq', type=int, default=5, metavar='N', 
    help='evaluation frequency (default: 25)')

parser.add_argument('--lr', type=float, default=1e-4, metavar='LR', 
    help='initial learning rate (default: 1e-4)')

parser.add_argument('--lr_add_cnst', type=float, default=1e-6, metavar='LR', 
    help='constant to add to learning rate each epoch (default: 1e-6)')

parser.add_argument('-m', action='store', dest='msg', nargs='*', type=str, required=True)

# Currently unused.
parser.add_argument('--momentum', type=float, default=0.9, metavar='M', 
    help='SGD momentum (default: 0.9)')

args = parser.parse_args()

if args.device >= 0:
    device = torch.device(f'cuda:{args.device}')
else:
    device = torch.device('cpu')

os.makedirs(f'{args.dir}/logs', exist_ok=True)
os.makedirs(f'{args.dir}/checkpoints', exist_ok=True)

torch.manual_seed(args.seed)
torch.cuda.manual_seed(args.seed)
np.random.seed(args.seed)
random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True

with open(os.path.join(args.dir, 'command.sh'), 'w') as f:
  f.write(' '.join(sys.argv))
  f.write('\n')

dims=[128, 128, 128]
if args.large_patch:
    dims=[160, 192, 128]

if args.cross_val:
    filenames=[]
    for (dirpath, dirnames, files) in os.walk(args.data_dir):
        filenames += [os.path.join(dirpath, file) for file in files if '.nii.gz' in file ]

        modes = [sorted([ f for f in filenames if "t1.nii.gz" in f ]),
                      sorted([ f for f in filenames if "t1ce.nii.gz" in f ]),
                      sorted([ f for f in filenames if "t2.nii.gz" in f ]),
                      sorted([ f for f in filenames if "flair.nii.gz" in f ]),
                        sorted([ f for f in filenames if "seg.nii.gz" in f ])

            ]
    joined_files = list(zip(*modes))

    random.shuffle(joined_files)
    split_idx = int(0.8*len(joined_files))
    train_split, val_split = joined_files[:split_idx], joined_files[split_idx:]
    def proc_split(split):
        modes = [[], [], [], []]
        segs = []

        for t1, t1ce, t2, flair, seg in split:
            modes[0].append(t1)
            modes[1].append(t1ce)
            modes[2].append(t2)
            modes[3].append(flair)
            segs.append(seg)
        return modes, segs

    train_modes, train_segs = proc_split(train_split)
    train_data = BraTSTrainDataset(args.data_dir, dims=dims, augment_data=args.augment_data,
            modes=train_modes, segs=train_segs)
    trainloader = DataLoader(train_data, batch_size=args.batch_size, 
                            shuffle=True, num_workers=args.num_workers)

    val_modes, val_segs = proc_split(val_split)
    val_data = BraTSTrainDataset(args.data_dir, dims=dims, augment_data=False,
            modes=val_modes, segs=val_segs)
    valloader = DataLoader(val_data, batch_size=args.batch_size, 
                            shuffle=True, num_workers=args.num_workers)
else:
    # train without cross_val
    train_data = BraTSTrainDataset(args.data_dir, dims=dims, augment_data=args.augment_data)
    trainloader = DataLoader(train_data, batch_size=args.batch_size, 
                            shuffle=True, num_workers=args.num_workers)
    val_data = BraTSTrainDataset(args.data_dir, dims=dims, augment_data=False)
    valloader = DataLoader(val_data, batch_size=args.batch_size, 
                            shuffle=True, num_workers=args.num_workers)


if args.model == 'MonoUNet':
    model = MonoUNet()
    loss = losses.AvgDiceLoss()
if args.model == 'CascadeNet':
    model = CascadeNet()
    loss = losses.CascadeAvgDiceLoss()
if args.model == 'VAEReg':
    model = VAEReg()
    loss = losses.VAEDiceLoss()

optimizer = \
    optim.Adam(model.parameters(), lr=args.lr, weight_decay=args.wd)

model = model.to(device)

start_epoch = 0
if args.resume:
    print(f'Resume training from {args.resume}')
    checkpoint = torch.load(args.resume)
    start_epoch = checkpoint["epoch"]
    model.load_state_dict(checkpoint["state_dict"])
    optimizer.load_state_dict(checkpoint["optimizer"])    

if args.pretrain:
    print(f'Begin training from pretrained model {args.pretrain}')
    checkpoint = torch.load(args.pretrain)
    model.load_state_dict(checkpoint["state_dict"])

writer = SummaryWriter(log_dir=f'{args.dir}/logs')
scheduler = None

swa_start = 1
opt = None
if args.swa:
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    opt = SWA(optimizer, swa_start=swa_start, swa_freq=1, swa_lr=0.05)
else:
    # this must occur before giving the optimizer to amp
    lmbda = lambda epoch : (1 - (epoch / args.epochs)) ** 0.9
    scheduler = optim.lr_scheduler.MultiplicativeLR(optimizer, lr_lambda=lmbda)
    #scheduler = PolynomialLR(optimizer, args.epochs, last_epoch=start_epoch-1)

# model has to be on device before passing to amp
if args.mixed_precision:
    # Allow Amp to perform casts as required by the opt_level
    model, optimizer = amp.initialize(model, optimizer, opt_level="O1")

# TODO: optimizer factory, allow for SGD with momentum etx.
columns = ['set', 'ep', 'loss', 'dice_et', 'dice_wt','dice_tc', \
   'time', 'mem_usage']

#lr = args.lr
for epoch in range(start_epoch, args.epochs):
    time_ep = time.time()
    model.train()

    if args.swa:
        train(model, 
                loss, 
                opt, 
                trainloader, 
                device, 
                mixed_precision=args.mixed_precision,
                debug=args.debug)
    else:
         train(model, 
                loss, 
                optimizer, 
                trainloader, 
                device, 
                mixed_precision=args.mixed_precision,
                debug=args.debug)
       
    if args.swa and epoch > swa_start:
        opt.swap_swa_sgd()

    if (epoch + 1) % args.save_freq == 0:
        if args.swa:
            opt.bn_update(trainloader, model)
        save_checkpoint(
                f'{args.dir}/checkpoints',
                epoch + 1,
                state_dict=model.state_dict(),
                optimizer=optimizer.state_dict(),
                msg=args.msg
                )
    
    if (epoch + 1) % args.eval_freq == 0:
        if args.swa:
            opt.bn_update(trainloader, model)
        # Evaluate on training data
        #train_val = validate(model, loss, trainloader, device)
        #time_ep = time.time() - time_ep
        #memory_usage = torch.cuda.memory_allocated() / (1024.0 ** 3)
        #train_values = ['train', epoch + 1, lr*1000, train_val['loss'].data] \
        #  + train_val['dice'].tolist()\
        #  + [ time_ep, memory_usage] 
        #eval_values = ['eval', epoch + 1, lr*1000, eval_val['loss'].data] \
        #  + eval_val['dice'].tolist()\
        #  + [ time_ep, memory_usage] 

        #table = tabulate.tabulate([train_values, eval_values], 
        #        columns, tablefmt="simple", floatfmt="8.4f")
        #print(table)
        model.eval()
        eval_val = validate(model, loss, valloader, device, debug=args.debug)

        writer.add_scalar(f'{args.dir}/logs/loss/eval', eval_val['loss'], epoch)
        et, wt, tc = eval_val['dice']
        writer.add_scalar(f'{args.dir}/logs/dice/eval/et', et, epoch)
        writer.add_scalar(f'{args.dir}/logs/dice/eval/wt', wt, epoch)
        writer.add_scalar(f'{args.dir}/logs/dice/eval/tc', tc, epoch)
        #writer.add_scalar(f'{args.dir}/logs/dice/eval/et_lr', et, lr)
        #writer.add_scalar(f'{args.dir}/logs/dice/eval/wt_lr', wt, lr)
        #writer.add_scalar(f'{args.dir}/logs/dice/eval/tc_lr', tc, lr)

        time_ep = time.time() - time_ep
        memory_usage = torch.cuda.memory_allocated() / (1024.0 ** 3)

        eval_values = ['eval', epoch + 1, eval_val['loss'].data] \
          + eval_val['dice'].tolist()\
          + [ time_ep, memory_usage] 

        table = tabulate.tabulate([eval_values], 
                columns, tablefmt="simple", floatfmt="8.4f")
        print(table)
   
        # Log validation
        #writer.add_scalar(f'{args.dir}/logs/loss/train', train_val['loss'], epoch)
        #et, wt, tc = train_val['dice']
        #writer.add_scalar(f'{args.dir}/logs/dice/train/et', et, epoch)
        #writer.add_scalar(f'{args.dir}/logs/dice/train/wt', wt, epoch)
        #writer.add_scalar(f'{args.dir}/logs/dice/train/tc', tc, epoch)
        #writer.add_scalar(f'{args.dir}/logs/dice/train/et_lr', et, lr)
        #writer.add_scalar(f'{args.dir}/logs/dice/train/wt_lr', wt, lr)
        #writer.add_scalar(f'{args.dir}/logs/dice/train/tc_lr', tc, lr)

    writer.flush()
    if not args.swa:
        scheduler.step()

