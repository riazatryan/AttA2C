import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Categorical

# todo: handle .cuda() on a high-level, not in each network separately

def init(module, weight_init, bias_init, gain=1):
    weight_init(module.weight.data, gain=gain)
    bias_init(module.bias.data)
    return module

class ConvBlock(nn.Module):

    def __init__(self, ch_in=4):
        """
        A basic block of convolutional layers,
        consisting: - 4 Conv2d
                    - LeakyReLU (after each Conv2d)
                    - currently also an AvgPool2d (I know, a place for me is reserved in hell for that)

        :param ch_in: number of input channels, which is equivalent to the number
                      of frames stacked together
        """
        super().__init__()

        # constants
        self.num_filter = 32
        self.size = 3
        self.stride = 2
        self.pad = self.size // 2

        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0), nn.init.calculate_gain('leaky_relu'))
        # layers
        self.conv1 = init_(nn.Conv2d(ch_in, self.num_filter, self.size, self.stride, self.pad))
        self.conv2 = init_(nn.Conv2d(self.num_filter, self.num_filter, self.size, self.stride, self.pad))
        self.conv3 = init_(nn.Conv2d(self.num_filter, self.num_filter, self.size, self.stride, self.pad))
        self.conv4 = init_(nn.Conv2d(self.num_filter, self.num_filter, self.size, self.stride, self.pad))

    def forward(self, x):
        x = F.leaky_relu(self.conv1(x))
        x = F.leaky_relu(self.conv2(x))
        x = F.leaky_relu(self.conv3(x))
        x = F.leaky_relu(self.conv4(x))

        x = nn.AvgPool2d(2)(x)  # needed as the input image is 84x84, not 42x42
        # return torch.flatten(x)
        return x.view(x.shape[0], -1)  # retain batch size

class FeatureEncoderNet(nn.Module):
    def __init__(self, n_stack, in_size, is_lstm=True):
        """
        Network for feature encoding

        :param n_stack: number of frames stacked beside each other (passed to the CNN)
        :param in_size: input size of the LSTMCell if is_lstm==True else it's the output size
        :param is_lstm: flag to indicate wheter an LSTMCell is included after the CNN
        """
        super().__init__()
        # constants
        self.in_size = in_size
        self.h1 = 256
        self.is_lstm = is_lstm  # indicates whether the LSTM is needed

        # layers
        self.conv = ConvBlock(ch_in=n_stack)
        if self.is_lstm:
            self.lstm = nn.LSTMCell(input_size=self.in_size, hidden_size=self.h1)

    def reset_lstm(self, buf_size=None, reset_indices=None):
        """
        Resets the inner state of the LSTMCell

        :param reset_indices: boolean list of the indices to reset (if True then that column will be zeroed)
        :param buf_size: buffer size (needed to generate the correct hidden state size)
        :return:
        """
        if self.is_lstm:
            with torch.no_grad():
                if reset_indices is None:
                    self.h_t1 = self.c_t1 = torch.zeros(buf_size,
                                                        self.h1).cuda() if torch.cuda.is_available() else torch.zeros(
                            buf_size,
                            self.h1)
                else:
                    resetTensor = torch.from_numpy(reset_indices.astype(np.uint8))
                    if resetTensor.sum():
                        self.h_t1 = (1 - resetTensor.view(-1, 1)).float().cuda() * self.h_t1
                        self.c_t1 = (1 - resetTensor.view(-1, 1)).float().cuda() * self.c_t1

    def forward(self, x):
        """
        In: [s_t]
            Current state (i.e. pixels) -> 1 channel image is needed

        Out: phi(s_t)
            Current state transformed into feature space

        :param x: input data representing the current state
        :return:
        """
        x = self.conv(x)

        # return self.lin(x)

        if self.is_lstm:
            x = x.view(-1, self.in_size)
            self.h_t1, self.c_t1 = self.lstm(x, (self.h_t1, self.c_t1))  # h_t1 is the output
            return self.h_t1  # [:, -1, :]#.reshape(-1)

        else:
            return x.view(-1, self.in_size)

class InverseNet(nn.Module):
    def __init__(self, num_actions, feat_size=288):
        """
        Network for the inverse dynamics

        :param num_actions: number of actions, pass env.action_space.n
        :param feat_size: dimensionality of the feature space (scalar)
        """
        super().__init__()

        # constants
        self.feat_size = feat_size
        self.fc_hidden = 256
        self.num_actions = num_actions

        # layers
        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0))
        self.fc1 = init_(nn.Linear(self.feat_size * 2, self.fc_hidden))
        self.fc2 = init_(nn.Linear(self.fc_hidden, self.num_actions))

    def forward(self, x):
        """
        In: torch.cat((phi(s_t), phi(s_{t+1}), 1)
            Current and next states transformed into the feature space,
            denoted by phi().

        Out: \hat{a}_t
            Predicted action

        :param x: input data containing the concatenated current and next states, pass
                  torch.cat((phi(s_t), phi(s_{t+1}), 1)
        :return:
        """
        return self.fc2(self.fc1(x))

class ForwardNet(nn.Module):

    def __init__(self, in_size):
        """
        Network for the forward dynamics

        :param in_size: size(feature_space) + size(action_space)
        """
        super().__init__()

        # constants
        self.in_size = in_size
        self.fc_hidden = 256
        self.out_size = 288

        # layers
        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0))
        self.fc1 = init_(nn.Linear(self.in_size, self.fc_hidden))
        self.fc2 = init_(nn.Linear(self.fc_hidden, self.out_size))

    def forward(self, x):
        """
        In: torch.cat((phi(s_t), a_t), 1)
            Current state transformed into the feature space,
            denoted by phi() and current action

        Out: \hat{phi(s_{t+1})}
            Predicted next state (in feature space)

        :param x: input data containing the concatenated current state in feature space
                  and the current action, pass torch.cat((phi(s_t), a_t), 1)
        :return:
        """
        return self.fc2(self.fc1(x))

class AdversarialHead(nn.Module):
    def __init__(self, feat_size, num_actions):
        """
        Network for exploiting the forward and inverse dynamics

        :param feat_size: size of the feature space
        :param num_actions: size of the action space, pass env.action_space.n
        """
        super().__init__()

        # constants
        self.feat_size = feat_size
        self.num_actions = num_actions

        # networks
        self.fwd_net = ForwardNet(self.feat_size + self.num_actions)
        self.inv_net = InverseNet(self.num_actions, self.feat_size)

    def forward(self, current_feature, next_feature, action):
        """

        :param current_feature: current encoded state
        :param next_feature: next encoded state
        :param action: current action
        :return: next_feature_pred (estimate of the next state in feature space),
                 action_pred (estimate of the current action)
        """

        """Forward dynamics"""
        # predict next encoded state

        # encode the current action into a one-hot vector
        action_one_hot = torch.zeros(action.shape[0], self.num_actions).scatter_(1, action.long().cpu().view(-1, 1), 1)

        if torch.cuda.is_available():
            action_one_hot = action_one_hot.cuda()
        # set_trace()

        fwd_in = torch.cat((current_feature, action_one_hot), 1)
        next_feature_pred = self.fwd_net(fwd_in)

        """Inverse dynamics"""
        # predict the action between s_t and s_t1
        inv_in = torch.cat((current_feature, next_feature), 1)
        action_pred = self.inv_net(inv_in)

        return next_feature_pred, action_pred

class ICMNet(nn.Module):
    def __init__(self, n_stack, num_actions, in_size=288, feat_size=256):
        """
        Network implementing the Intrinsic Curiosity Module (ICM) of https://arxiv.org/abs/1705.05363

        :param n_stack: number of frames stacked
        :param num_actions: dimensionality of the action space, pass env.action_space.n
        :param in_size: input size of the AdversarialHeads
        :param feat_size: size of the feature space
        """
        super().__init__()

        # constants
        self.in_size = in_size  # pixels i.e. state
        self.feat_size = feat_size
        self.num_actions = num_actions

        # networks
        self.feat_enc_net = FeatureEncoderNet(n_stack, self.in_size, is_lstm=False)
        self.pred_net = AdversarialHead(self.in_size, self.num_actions)  # goal: minimize prediction error
        self.policy_net = AdversarialHead(self.in_size, self.num_actions)  # goal: maximize prediction error
        # (i.e. predict states which can contain new information)

    def forward(self, current_state, next_state, action):
        """

        feature: current encoded state
        next_feature: next encoded state

        :param current_state: current state
        :param next_state: next state
        :param action: current action
        :return:
        """

        """Encode the states"""
        feature = self.feat_enc_net(current_state)
        next_feature = self.feat_enc_net(next_state)

        """ HERE COMES THE NEW THING (currently commented out)"""
        next_feature_pred, action_pred = self.pred_net(feature, next_feature, action)
        # phi_t1_policy, a_t_policy = self.policy_net_net(feature, next_feature, a_t)

        return next_feature, next_feature_pred, action_pred  # (next_feature_pred, action_pred), (phi_t1_policy, a_t_policy)

class A2CNet(nn.Module):
    def __init__(self, n_stack, num_envs, num_actions, in_size=288, writer=None):
        """
        Implementation of the Advantage Actor-Critic (A2C) network

        :param num_envs:
        :param n_stack: number of frames stacked
        :param num_actions: size of the action space, pass env.action_space.n
        :param in_size: input size of the LSTMCell of the FeatureEncoderNet
        """
        super().__init__()

        self.writer = writer
        self.num_step = 0

        # constants
        self.in_size = in_size  # in_size
        self.num_actions = num_actions

        # networks
        init_ = lambda m: init(m, nn.init.orthogonal_, lambda x: nn.init.
                               constant_(x, 0))

        self.feat_enc_net = FeatureEncoderNet(n_stack, self.in_size)
        self.actor = init_(nn.Linear(self.feat_enc_net.h1, self.num_actions))  # estimates what to do
        self.critic = init_(nn.Linear(self.feat_enc_net.h1,
                                      1))  # estimates how good the value function (how good the current state is)

        # init LSTM buffers with the number of the environments
        self._set_recurrent_buffers(num_envs)

    def _set_recurrent_buffers(self, buf_size):
        """
        Initializes LSTM buffers with the proper size

        :param buf_size: size of the recurrent buffer
        :return:
        """
        self.feat_enc_net.reset_lstm(buf_size=buf_size)

    def reset_recurrent_buffers(self, reset_indices):
        """

        :param reset_indices: boolean numpy array containing True at the indices which
                              should be reset
        :return:
        """
        self.feat_enc_net.reset_lstm(reset_indices=reset_indices)

    def forward(self, state):
        """

        feature: current encoded state

        :param state: current state
        :return:
        """

        # encode the state
        feature = self.feat_enc_net(state)

        # calculate policy and value function
        policy = self.actor(feature)
        value = self.critic(feature)

        if self.writer is not None:
            self.writer.add_histogram("feature", feature.detach())
            self.writer.add_histogram("policy", policy.detach())
            self.writer.add_histogram("value", value.detach())

        self.num_step += 1

        return policy, torch.squeeze(value)

    def get_action(self, state):
        """
        Method for selecting the next action

        :param state: current state
        :return: tuple of (action, log_prob_a_t, value)
        """

        """Evaluate the A2C"""
        policy, value = self(state)  # use A3C to get policy and value

        """Calculate action"""
        # 1. convert policy outputs into probabilities
        # 2. sample the categorical  distribution represented by these probabilities
        action_prob = F.softmax(policy, dim=-1)
        cat = Categorical(action_prob)
        action = cat.sample()

        return (action, cat.log_prob(action), cat.entropy().mean(), value)