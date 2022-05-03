import argparse
import os
import shutil
import time
import random
import resnet
import numpy as np
import math
import copy
import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.utils.data
import torchvision.transforms as transforms
import torchvision.datasets as datasets
from opacus3.utils import module_modification
from opacus3 import PrivacyEngine
from tqdm import tqdm

model_names = sorted(name for name in resnet.__dict__
                     if name.islower() and not name.startswith("__")
                     and name.startswith("resnet")
                     and callable(resnet.__dict__[name]))

# print("resnet20, method 0 L2, gap =0.15, randommix, grouping=3, clip_norm, 1 batch_size: 600, noise 0.004, start_rate 0.01")
# print("resnet20, comparison 0.0015 L_1 3 0.01 tau = 0 ")


dev = 0
device = torch.device('cuda:0')

batch_select = 500
test_batch = 256
keep_bn = True
use_trunc = False
use_prune = True
use_norm = True
use_mix = True
use_PEStep = False
noise_mul = 0
max_val = 3
start_lr = 0.1
num_epoch = 200
gap_0 = 0.2
prune_percentage_0 = 99

# not used parameter
clip_method = 1
clip_type = 2.0
clip_norm = 20
true_batch = 1

if use_norm:
   print("Use Norm", use_norm, "CLIP NORM",clip_norm ,"Batchnorm", keep_bn ,"Use Pruning", use_prune, prune_percentage_0,"Use Mix", use_mix, gap_0, "Noise", noise_mul, "Num of Epochs", num_epoch, "Device", dev)

if use_trunc:
   print("Use Trunc", use_trunc, "Max Value",max_val,"Batchnorm", keep_bn ,"Use Pruning", use_prune, prune_percentage_0,"Use Mix", use_mix, gap_0,
                     "Noise", noise_mul, "Num of Epochs", num_epoch, "Device", dev)

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
parser.add_argument('-b', '--batch-size', default=batch_select, type=int,
                    metavar='N', help='mini-batch size (default: 128)')
parser.add_argument('--lr', '--learning-rate', default=start_lr, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--print-freq', '-p', default=10, type=int,
                    metavar='N', help='print frequency (default: 50)')
parser.add_argument('--resume', default='', type=str, metavar='PATH', help='path to latest checkpoint (default: none)')
#parser.add_argument('--resume', default='save_temp/checkpointNew.th', type=str, metavar='PATH',
#                    help='path to latest checkpoint (default: none)')

parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
# parser.add_argument('--pretrained', dest='pretrained', action='store_true',  help='use pre-trained model')
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

    # model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=True)
    # model2 = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=True)
    model = resnet.__dict__[args.arch]()
    model2 = resnet.__dict__[args.arch]()
    store_model = resnet.__dict__[args.arch]()
    if not keep_bn:
        model = module_modification.convert_batchnorm_modules(model)
        model2 = module_modification.convert_batchnorm_modules(model2)
    # model = torch.hub.load('pytorch/vision:v0.10.0', 'resnet18', pretrained=False)

    # optionally resume from a checkpoint
    if args.resume and keep_bn:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            temp_dict = model.state_dict()
            for name in temp_dict.keys():
                if ('bn' not in name or temp_dict[name].shape != checkpoint['state_dict'][name].shape):
                    checkpoint['state_dict'][name] = temp_dict[name]
            model.load_state_dict(checkpoint['state_dict'])
            model2.load_state_dict(checkpoint['state_dict'])
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))
    model.cuda(device)
    model2.cuda(device)
    store_model.cuda(device)

    if keep_bn:
        for name, param in model.named_parameters():
            if "bn" in name:
                param.require_grad = False
        for name, param in model2.named_parameters():
            if "bn" in name:
                param.require_grad = False

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
        batch_size=test_batch, shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(device)
    # criterion = nn.MSELoss().cuda(device)

    if args.half:
        model.half()
        model2.half()
        criterion.half()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)
    optimizer2 = torch.optim.SGD(model2.parameters(), args.lr,
                                 momentum=args.momentum,
                                 weight_decay=args.weight_decay)
    optimizer3 = torch.optim.SGD(store_model.parameters(), args.lr,
                                 momentum=args.momentum,
                                 weight_decay=args.weight_decay)
    privacy_engine = PrivacyEngine(model, batch_size=batch_select, sample_size=len(train_loader.dataset),
                                   alphas=range(2, 32), noise_multiplier=noise_mul, max_grad_norm=clip_norm)
    privacy_engine.attach(optimizer)
    privacy_engine2 = PrivacyEngine(model2, batch_size=batch_select, sample_size=len(train_loader.dataset),
                                    alphas=range(2, 32), noise_multiplier=noise_mul, max_grad_norm=clip_norm)
    privacy_engine2.attach(optimizer2)

    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=[50, 80],
                                                        last_epoch=args.start_epoch - 1)
    lr_scheduler2 = torch.optim.lr_scheduler.MultiStepLR(optimizer2, milestones=[50, 80],
                                                         last_epoch=args.start_epoch - 1)
    lr_scheduler3 = torch.optim.lr_scheduler.MultiStepLR(optimizer3, milestones=[40, 80],
                                                         last_epoch=args.start_epoch - 1)

    # lr_scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.98, last_epoch=args.start_epoch - 1)

    if args.arch in ['resnet1202', 'resnet110']:
        # for resnet1202 original paper uses lr=0.01 for first 400 minibatches for warm-up
        # then switch back. In this setup it will correspond for first epoch.
        for param_group in optimizer.param_groups:
            param_group['lr'] = args.lr * 0.1
        for param_group2 in optimizer2.param_groups:
            param_group2['lr'] = args.lr * 0.1
        for param_group3 in optimizer3.param_groups:
            param_group3['lr'] = args.lr * 0.1

    if args.evaluate:
        validate(val_loader, model, criterion)
        validate(val_loader, model2, criterion)
        return

    for epoch in range(args.start_epoch, args.epochs):
        print(epoch)
        # train for one epoch
        print('current lr {:.5e}'.format(optimizer.param_groups[0]['lr']))
        if epoch < 100:
            prune_percentage = prune_percentage_0
        else:
            prune_percentage = prune_percentage_0

        if epoch < 30:
            gap_rate = gap_0
        elif epoch < 60:
            gap_rate = gap_0
        else:
            gap_rate = gap_0

        if epoch < 70:
            noise_scale = noise_mul
        else:
            noise_scale = noise_mul


        train(train_loader, model, model2, store_model,
              criterion, optimizer, optimizer2, optimizer3,
              epoch, prune_percentage, gap_rate, noise_scale)
        lr_scheduler.step()
        lr_scheduler2.step()
        lr_scheduler3.step()

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


def train(train_loader, model, model2, store_model, criterion,
          optimizer, optimizer2, optimizer3,
          epoch, prune_percentage, gap_rate, noise_scale):
    """
        Run one train epoch
    """
    # if epoch < 30:
    #    use_mask = False
    # else:
    #    use_mask = True

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()

    # switch to train mode
    model.train()
    model2.train()
    store_model.train()

    end = time.time()
    lr_temp = optimizer.param_groups[0]['lr']
    tau = lr_temp * gap_rate
    print(tau)
    loss_val = 0
    for i, (input, target) in enumerate(train_loader):

        if i % 2 == 0:
            temp_model = model
            temp_model2 = model2
            temp_optimizer = optimizer
        else:
            temp_model = model2
            temp_model2 = model
            temp_optimizer = optimizer2

        for name, param in temp_model.named_parameters():
            if "bn" in name:
                param.require_grad = False

        # measure data loading time
        data_time.update(time.time() - end)

        target = target.cuda(device)
        input_var = input.cuda(device)
        target_var = target
        if args.half:
            input_var = input_var.half()

        if keep_bn:
            temp_dict = copy.deepcopy(temp_model.state_dict())
            store_model.load_state_dict(temp_dict)

            # store_model.eval()
            store_output = store_model(input_var)
            store_loss = criterion(store_output, target_var)
            store_model.zero_grad()
            store_loss.backward()
            optimizer3.step()
            store_dict = store_model.state_dict()

            for name in temp_dict.keys():
                if "bn" in name:
                    temp_dict[name] = store_dict[name]
            temp_model.load_state_dict(temp_dict)

        # temp_model.eval()
        output = temp_model(input_var)
        loss = criterion(output, target_var)
        temp_optimizer.zero_grad()
        loss.backward()

        # calculate the percentile value and the mask for each param
        mask = {}
        if use_prune:
            all_param = torch.empty(0).cuda(dev)
            percent_noise_scale = 0
            for name, param in temp_model.named_parameters():
                if "bn" in name:
                    continue
                laplace_dist = torch.distributions.laplace.Laplace(0, 1)
                noise1 = laplace_dist.sample(param.grad.shape).cuda(device) * percent_noise_scale
                sum_grad = param.grad_sample.sum(0).squeeze() / batch_select
                # we use mask to temporarily store the sum of the gradient, it will be switched to mask later
                mask[name] = sum_grad + noise1
                all_param = torch.cat((all_param, torch.flatten(mask[name])), 0)
            percentile_value = np.percentile(abs(all_param.cpu().numpy()), prune_percentage)

            for name, param in temp_model.named_parameters():
                if "bn" in name:
                    continue
                sum_grad = mask[name].cpu().numpy()
                temp_mask = np.where(abs(sum_grad) < percentile_value, 0, 1)
                mask[name] = torch.from_numpy(temp_mask).cuda(device)
                large_mask = mask[name].unsqueeze(0).expand(param.grad_sample.shape)
                #print(large_mask)
                param.grad_sample = param.grad_sample * large_mask
        else:
            for name, param in temp_model.named_parameters():
                mask[name] = torch.ones(param.grad.shape).cuda(device)

        true_grad = {}
        for name, param in temp_model.named_parameters():
            if "bn" in name:
                continue
            true_grad[name] = torch.squeeze(torch.sum(param.grad_sample, 0))

        if use_trunc:
            for name, param in temp_model.named_parameters():
                if "bn" not in name:
                    param.grad_sample = param.grad_sample.clamp(min=-max_val, max=max_val)

        if use_mix:
            temp_dict = temp_model.state_dict()
            temp_dict2 = temp_model2.state_dict()
            for name, param in temp_model.named_parameters():
                if "bn" in name or name not in temp_dict.keys():
                    continue
                gap_opr = torch.abs(temp_dict[name] - temp_dict2[name])
                if torch.min(gap_opr) < tau:
                    gap_opr = gap_opr.clamp(min = 0, max = tau)
                    gap_opr = torch.add(-gap_opr, tau)
                    sign_opr = torch.sign(temp_dict[name] - temp_dict2[name])
                    # sign_opr[sign_opr == 0] = 1
                    sign_opr = sign_opr * mask[name]
                    temp_dict[name] = temp_dict[name].add(gap_opr * sign_opr / 2)
                    temp_dict2[name] = temp_dict2[name].add(-gap_opr * sign_opr / 2)

                dictShape = temp_dict[name].shape
                oness = torch.ones(dictShape).cuda(device)
                alpha1 = torch.rand(dictShape).cuda(device)
                alpha1 = mask[name] * alpha1 + (oness - mask[name]) * oness / 2
                temp_dict[name] = alpha1 * temp_dict[name] + (oness - alpha1) * temp_dict2[name]
            temp_model.load_state_dict(temp_dict)

        if not keep_bn and use_PEStep:
            temp_optimizer.step()
        else:
            if use_norm:
                sum_norms = torch.zeros(batch_select).cuda(device)
                for name, param in temp_model.named_parameters():
                    if "bn" in name:
                        continue
                    norms = param.grad_sample.view(len(param.grad_sample), -1).norm(2, -1)
                    #norms = torch.norm(param.grad_sample, p = 2, dim = -1)
                    sum_norms += norms * norms
                sum_norms = torch.sqrt(sum_norms)
                sum_norms = torch.add(0.000000001, sum_norms)
                # print(sum_norms)
                clip_factor = torch.ones(sum_norms.shape).cuda(device) * clip_norm / sum_norms
                clip_factor = clip_factor.clamp(max=1)

                for name, param in temp_model.named_parameters():
                    if "bn" in name:
                        continue
                    #param.grad = torch.zeros(param.grad.shape).cuda(device)
                    #for j in range(batch_select):
                    #    param.grad += torch.mul(param.grad_sample[j], clip_factor[j]) / batch_select
                    param.grad = torch.einsum("i,i...", clip_factor, param.grad_sample) / batch_select
            else:
                for name, param in temp_model.named_parameters():
                    if "bn" in name:
                        continue
                    param.grad = param.grad_sample.sum(0).squeeze() / batch_select

            for name, param in temp_model.named_parameters():
                if "bn" in name:
                    continue
                if clip_type == 2.0:
                    noise = torch.randn(param.grad.shape)
                if clip_type == 1.0:
                    laplace_dist = torch.distributions.laplace.Laplace(0, 1)
                    noise = laplace_dist.sample(param.grad.shape)
                param.grad += torch.mul(noise, noise_scale).cuda(device) * mask[name]

            # calculate the product between param.grad and true_grad
            product = torch.tensor(0.0).cuda(device)
            true_norm = torch.tensor(0.0).cuda(device)
            param_norm = torch.tensor(0.0).cuda(device)
            for name, param in temp_model.named_parameters():
                if 'bn' in name:
                    continue
                product += torch.sum(param.grad * true_grad[name])
                norms = true_grad[name].norm(p = 2)
                true_norm += norms * norms
                norms = param.grad.norm(p = 2)
                param_norm += norms * norms
            if i > 0 and i % (args.print_freq * true_batch) == 0:
                print(product / torch.sqrt(true_norm * param_norm))

            temp_optimizer.original_step()

        output = output.float()
        loss = loss.float()
        # measure accuracy and record loss
        prec1 = accuracy(output.data, target)[0]
        losses.update(loss.item(), input.size(0))
        top1.update(prec1.item(), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()
        if i > 0 and i % (args.print_freq * true_batch) == 0:
            print('Epoch: [{0}][{1}/{2}]\t'
                  'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                  'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                  'Loss {loss.avg:.4f}\t'
                  'Prec@1 {top1.avg:.3f}'.format(
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=top1))


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
