'''Train CIFAR10 with PyTorch.'''
import torch
import torch.distributed as dist
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.utils.data import DataLoader, Dataset
from torch.utils.data.distributed import DistributedSampler
import torch.optim as optim
import torch.backends.cudnn as cudnn

import torchvision
import torchvision.transforms as transforms

import os
import argparse

from models import *
from utils import progress_bar


parser = argparse.ArgumentParser(description='PyTorch CIFAR10 Training')
parser.add_argument('--lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--trainbs', default=128, type=int, help='trainloader batch size')
parser.add_argument('--testbs', default=100, type=int, help='testloader batch size')
parser.add_argument('--resume', '-r', action='store_true', help='resume from checkpoint')
parser.add_argument('--local_rank', type=int, default=-1, help='DDP parameter, do not modify')
args = parser.parse_args()
ngpus = torch.cuda.device_count()
cuda = torch.cuda.is_available()

# initialize PyTorch distributed using environment variables
# (you could also do this more explicitly by specifying `rank` and `world_size`,
# but I find using environment variables makes it so that you can easily use the same script on different machines)
dist.init_process_group(backend='nccl', init_method='env://')

torch.cuda.set_device(args.local_rank)
device = torch.device('cuda', args.local_rank)

best_acc = 0  # best test accuracy
start_epoch = 0  # start from epoch 0 or last checkpoint epoch

# Data
if args.local_rank == 0:
    print('==> Preparing data..')
transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

trainset = torchvision.datasets.CIFAR10(root='./data', train=True, download=True, transform=transform_train)
sampler = DistributedSampler(trainset)
trainloader = torch.utils.data.DataLoader(trainset, sampler=sampler, batch_size=args.trainbs, num_workers=2)

testset = torchvision.datasets.CIFAR10(root='./data', train=False, download=True, transform=transform_test)
testloader = torch.utils.data.DataLoader(testset, batch_size=args.testbs, shuffle=False, num_workers=2)


classes = ('plane', 'car', 'bird', 'cat', 'deer', 'dog', 'frog', 'horse', 'ship', 'truck')

# Model
if args.local_rank == 0:
    print('==> Building model..')
# net = VGG('VGG16')
net = ResNet50()
# net = PreActResNet18()
# net = GoogLeNet()
# net = DenseNet121()
# net = ResNeXt29_2x64d()
# net = ResNeXt29_32x4d()
# net = MobileNet()
# net = MobileNetV2()
# net = DPN92()
# net = ShuffleNetG2()
# net = SENet18()
# net = ShuffleNetV2(1)
# net = EfficientNetB0()
net_name = net.name
save_path = './checkpoint/{0}_ckpt.pth'.format(net.name)

if args.resume:
    # Load best checkpoint trained last time.
    if args.local_rank == 0:
        print('==> Resuming from checkpoint..')
    assert os.path.isdir('checkpoint'), 'Error: no checkpoint directory found!'
    checkpoint = torch.load(save_path)
    net.load_state_dict(checkpoint['net'])
    best_acc = checkpoint['acc']
    start_epoch = checkpoint['epoch']

net = net.to(device)
net = nn.SyncBatchNorm.convert_sync_batchnorm(net)
if ngpus > 1:
    net = DDP(net, device_ids=[args.local_rank], output_device=args.local_rank)

criterion = nn.CrossEntropyLoss()
optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)
scheduler = optim.lr_scheduler.StepLR(optimizer, step_size=70, gamma=0.1)

# Training
def train(epoch):
    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = net(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        optimizer.step()

        train_loss += loss.item()
        _, predicted = outputs.max(1)
        total += targets.size(0)
        correct += predicted.eq(targets).sum().item()

        if args.local_rank == 0:
            progress_bar(batch_idx, len(trainloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                % (train_loss/(batch_idx+1), 100.*correct/total, correct, total))

def test(epoch):
    global best_acc
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            outputs = net(inputs)
            loss = criterion(outputs, targets)

            test_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            if args.local_rank == 0:
                progress_bar(batch_idx, len(testloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                    % (test_loss/(batch_idx+1), 100.*correct/total, correct, total))

    # Save checkpoint.
    acc = 100.*correct/total
    if acc > best_acc:
        if args.local_rank == 0:
            print('Saving ' + net_name + ' ..')
        state = {
            'net': net.module.state_dict() if ngpus>1 else net.state_dict(),
            'acc': acc,
            'epoch': epoch,
        }
        if not os.path.isdir('checkpoint'):
            os.mkdir('checkpoint')
        torch.save(state, save_path)
        best_acc = acc


for epoch in range(start_epoch, start_epoch+300):
    # In PyTorch 1.1.0 and later,
    # you should call them in the opposite order:
    # `optimizer.step()` before `lr_scheduler.step()`
    sampler.set_epoch(epoch)
    train(epoch)
    test(epoch)
    scheduler.step()  # 每隔100 steps学习率乘以0.1

print("\nTesting best accuracy:", best_acc)
