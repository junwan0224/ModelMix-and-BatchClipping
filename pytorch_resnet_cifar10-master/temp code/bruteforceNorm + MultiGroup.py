import argparse
import os
import shutil
import time
import random
import resnet
import numpy
import math

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import autograd_hacks


model_names = sorted(name for name in resnet.__dict__
                     if name.islower() and not name.startswith("__")
                     and name.startswith("resnet")
                     and callable(resnet.__dict__[name]))

#print("resnet20, method 0 L2, gap =0.15, randommix, grouping=3, clip_norm, 1 batch_size: 600, noise 0.004, start_rate 0.01")
#print("resnet20, comparison 0.0015 L_1 3 0.01 tau = 0 ")


dev = 1
device = torch.device('cuda:1')

norm_batch = 5
data_batch = 500
#noise_scale = 2/true_batch
noise_scale = 0
clip_method = 1
clip_type = 2.0
clip_norm = 1
start_lr = 0.01
gap_rate = 0.01
num_epoch = 300
print("L NORM:",clip_type,"Gap",gap_rate,"CLIP NORM",clip_norm,"Grouping",norm_batch,"Noise",noise_scale, "Num of Epochs", num_epoch, "Device", dev)



parser = argparse.ArgumentParser(description='Propert ResNets for CIFAR10 in pytorch')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet20',
                    choices=model_names,
                    help='model architecture: ' + ' | '.join(model_names) +
                         ' (default: resnet32)')
parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=num_epoch, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=data_batch, type=int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--lr', '--learning-rate', default=start_lr, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=50, type=int,
                    metavar='N', help='print frequency (default: 50)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--half', dest='half', action='store_true',
                    help='use half-precision(16-bit) ')
parser.add_argument('--save-dir', dest='save_dir',
                    help='The directory used to save the trained models',
                    default='save_temp', type=str)
parser.add_argument('--save-every', dest='save_every',
                    help='Saves checkpoints at every specified number of epochs',
                    type=int, default=10)
best_prec1 = 0




def main():
    global args, best_prec1, dev
    args = parser.parse_args()

    # Check the save_dir exists or not
    if not os.path.exists(args.save_dir):
        os.makedirs(args.save_dir)

    model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=False)
    model2 = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=False)

    #model = torch.nn.DataParallel(resnet.__dict__[args.arch](), device_ids=[dev, 1 - dev])
    model.cuda(device)
    #model2 = torch.nn.DataParallel(resnet.__dict__[args.arch](), device_ids=[dev, 1 - dev])
    model2.cuda(device)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            model2.load_state_dict(checkpoint['state_dict'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.evaluate, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    train_loader = torch.utils.data.DataLoader(
        datasets.CIFAR10(root='./data', train=True, transform=transforms.Compose([
            transforms.RandomHorizontalFlip(),
            transforms.RandomCrop(32, 4),
            transforms.ToTensor(),
            normalize,
        ]), download=True),
        batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)

    val_loader = torch.utils.data.DataLoader(
        datasets.CIFAR10(root='./data', train=False, transform=transforms.Compose([
            transforms.ToTensor(),
            normalize,
        ])),
        batch_size=128, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(device)

    if args.half:
        model.half()
        criterion.half()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    optimizer2 = torch.optim.SGD(model2.parameters(), args.lr,
                                 momentum=args.momentum,
                                 weight_decay=args.weight_decay)

    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
                                                        milestones=[150, 200, 250], last_epoch=args.start_epoch - 1)

    if args.arch in ['resnet1202', 'resnet110']:
        # for resnet1202 original paper uses lr=0.01 for first 400 minibatches for warm-up
        # then switch back. In this setup it will correspond for first epoch.
        for param_group in optimizer.param_groups:
            param_group['lr'] = args.lr * 0.1

    if args.evaluate:
        validate(val_loader, model, criterion)
        return

    for epoch in range(args.start_epoch, args.epochs):

        # train for one epoch
        print('current lr {:.5e}'.format(optimizer.param_groups[0]['lr']))
        train(train_loader, model, model2, criterion, optimizer, optimizer2, epoch)
        lr_scheduler.step()

        # evaluate on validation set
        prec1 = validate(val_loader, model, criterion)

        # remember best prec@1 and save checkpoint
        is_best = prec1 > best_prec1
        best_prec1 = max(prec1, best_prec1)

        if epoch > 0 and epoch % args.save_every == 0:
            save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
            }, is_best, filename=os.path.join(args.save_dir, 'checkpoint.th'))

        save_checkpoint({
            'state_dict': model.state_dict(),
            'best_prec1': best_prec1,
        }, is_best, filename=os.path.join(args.save_dir, 'model.th'))


def train(train_loader, model, model2, criterion, optimizer, optimizer2, epoch):
    """
        Run one train epoch
    """
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()
    model2.train()

    end = time.time()
    lr_temp = optimizer.param_groups[0]['lr']
    tau = lr_temp * gap_rate
    print(tau)
    sum_grad = {}
    total_param = 0
    changed_param = 0
    total_norm = 0
    norm_times = 0
    for i, (input, target) in enumerate(train_loader):

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(device)
        input_var = input.cuda(device)
        target_var = target
        if args.half:
            input_var = input_var.half()

        if i % true_batch == 0:
            sum_grad = {}
        if math.floor(i / true_batch) % 2 == 0:
            temp_model = model
            temp_model2 = model2
            temp_optimizer = optimizer
        else:
            temp_model = model2
            temp_model2 = model
            temp_optimizer = optimizer2

        autograd_hacks.add_hooks(temp_model)
        output = temp_model(input_var)
        loss = criterion(output, target_var)
        temp_optimizer.zero_grad()
        loss.backward()

        autograd_hacks.compute_grad1(temp_model)
        autograd_hacks.disable_hooks()
        # temp_optimizer.step()

        scale = 1
        
        norm_val = torch.nn.utils.clip_grad_norm_(temp_model.parameters(), max_norm=math.inf, norm_type=clip_type)
        total_norm += norm_val
        norm_times += 1

        if clip_method == 1:
            if norm_val.item() >= clip_norm:
                scale = clip_norm
            else:
                scale = norm_val.item()

        if i % true_batch == 0:
            for name, param in temp_model.named_parameters():
                sum_grad[name] = torch.mul(param.grad, scale)
        else:
            for name, param in temp_model.named_parameters():
                sum_grad[name] = sum_grad[name] + torch.mul(param.grad, scale)

        gap_mag = 0
        if (i+1) % true_batch == 0:
            temp_dict = temp_model.state_dict()
            temp_dict2 = temp_model2.state_dict()
            for j in temp_dict.keys():
                total_param += torch.numel(temp_dict[j])
                gap_opr = torch.abs(temp_dict[j] - temp_dict2[j])
                gap_mag +=  torch.sum(gap_opr)
                if (torch.min(gap_opr) < tau):
                    gap_opr = torch.clamp(gap_opr, 0, tau)
                    gap_opr = torch.add(-gap_opr, tau)
                    sign_opr = torch.sign(temp_dict[j] - temp_dict2[j])
                    sign_opr[sign_opr == 0] = 1
                    changed_param += torch.count_nonzero(gap_opr).item()
                    temp_dict[j] = torch.add(temp_dict[j], gap_opr * sign_opr / 2)
                    temp_dict2[j] = torch.add(temp_dict2[j], -gap_opr * sign_opr / 2)

                dictShape = temp_dict[j].shape
                alpha1 = torch.rand(dictShape).cuda(device)
                #alpha2 = torch.rand(dictShape).cuda(device)
                temp_dict[j] = alpha1 * temp_dict[j] + (torch.ones(dictShape).cuda(device) - alpha1) * temp_dict2[j]
                #temp_dict2[j] = alpha2 * temp_dict[j] + (torch.ones(dictShape).cuda(device) - alpha2) * temp_dict2[j]
            temp_model.load_state_dict(temp_dict)
            for name, param in temp_model.named_parameters():
                if clip_type == 2.0:
                   param.grad = torch.div(sum_grad[name], true_batch) + torch.mul(torch.randn(sum_grad[name].shape), noise_scale).cuda(device)
                if clip_type == 1.0:
                   laplace_dist = torch.distributions.laplace.Laplace(0, 1)
                   param.grad = torch.div(sum_grad[name], true_batch)+ torch.mul(laplace_dist.sample(sum_grad[name].shape), noise_scale).cuda(device)
            temp_optimizer.step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        if i > 0 and i % (args.print_freq * true_batch)  == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                  'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=top1))
            print("changed percentage: ", changed_param / total_param, "  average norm: ", total_norm / norm_times);
            if (i == args.print_freq * true_batch):
                print("total param: ", total_param, " average_gap: ", gap_mag/5849566)
    print("end of epoch, changed percentage: ", changed_param / total_param, "  average norm: ", total_norm / norm_times);

def validate(val_loader, model, criterion):
    """
    Run evaluation
    """
    batch_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    with torch.no_grad():
        for i, (input, target) in enumerate(val_loader):
            target = target.cuda(device)
            input_var = input.cuda(device)
            target_var = target.cuda(device)

            if args.half:
                input_var = input_var.half()

            # compute output
            output = model(input_var)
            loss = criterion(output, target_var)

            output = output.float()
            loss = loss.float()

            # measure accuracy and record loss
            prec1 = accuracy(output.data, target)[0]
            losses.update(loss.item(), input.size(0))
            top1.update(prec1.item(), input.size(0))

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if i % args.print_freq == 0:
                print('Test: [{0}/{1}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'Prec@1 {top1.val:.3f} ({top1.avg:.3f})'.format(
                    i, len(val_loader), batch_time=batch_time, loss=losses,
                    top1=top1))

    print(' * Prec@1 {top1.avg:.3f}'
          .format(top1=top1))

    return top1.avg


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """
    Save the training model
    """
    torch.save(state, filename)


class AverageMeter(object):
    """Computes and stores the average and current value"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    maxk = max(topk)
    batch_size = target.size(0)

    _, pred = output.topk(maxk, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))

    res = []
    for k in topk:
        correct_k = correct[:k].view(-1).float().sum(0)
        res.append(correct_k.mul_(100.0 / batch_size))
    return res


if __name__ == '__main__':
    main()