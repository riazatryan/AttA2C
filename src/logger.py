from os.path import abspath, dirname, join

import h5py
import imageio
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import torch
from mpl_toolkits.axes_grid1.inset_locator import mark_inset
from stable_baselines.common.cmd_util import make_atari_env
from stable_baselines.common.vec_env import VecFrameStack

from agent import ICMAgent
from utils import make_dir, numpy_ewma_vectorized_v2, plot_postprocess, print_init, series_indexer, \
    color4label, label_enum_converter, instance2label


class LogData(object):
    def __init__(self):
        self.mean = []
        self.std = []
        self.min = []
        self.max = []

    def log(self, sample):
        """

        :param sample: data for logging specified as a numpy.array
        :return:
        """
        self.mean.append(sample.mean())
        self.std.append(sample.std())
        self.min.append(sample.min())
        self.max.append(sample.max())

    def save(self, group):
        """

        :param group: the reference to the group level hierarchy of a .hdf5 file to save the data
        :return:
        """
        for key, val in self.__dict__.items():
            group.create_dataset(key, data=val)

    def load(self, group, decimate_step=100):
        """
        :param decimate_step:
        :param group: the reference to the group level hierarchy of a .hdf5 file to load
        :return:
        """
        # read in parameters
        # [()] is needed to read in the whole array if you don't do that,
        #  it doesn't read the whole data but instead gives you lazy access to sub-parts
        #  (very useful when the array is huge but you only need a small part of it).
        # https://stackoverflow.com/questions/10274476/how-to-export-hdf5-file-to-numpy-using-h5py
        self.mean = group["mean"][()][::decimate_step]
        self.std = group["std"][()][::decimate_step]
        self.min = group["min"][()][::decimate_step]
        self.max = group["max"][()][::decimate_step]

    def plot_mean_min_max(self, label):
        plt.fill_between(range(len(self.mean)), self.max, self.min, alpha=.5)
        plt.plot(self.mean, label=label)

    def plot_mean_std(self, label):
        mean = np.array(self.mean)
        plt.fill_between(range(len(self.mean)), mean + self.std, mean - self.std, alpha=.5)
        plt.plot(self.mean, label=label)


class TemporalLogger(object):
    def __init__(self, env_name, timestamp, log_dir, *args):
        """
        Creates a TemporalLogger object. If the folder structure is nonexistent, it will also be created
        :param *args:
        :param env_name: name of the environment
        :param timestamp: timestamp as a string
        :param log_dir: logging directory, if it is None, then logging will be at the same hierarchy level as src/
        """
        super().__init__()
        self.timestamp = timestamp

        # file structure
        self.base_dir = join(dirname(dirname(abspath(__file__))), "log") if log_dir is None else log_dir
        self.data_dir = join(self.base_dir, env_name)
        make_dir(self.base_dir)
        make_dir(self.data_dir)

        # data
        for data in args:
            self.__dict__[data] = LogData()

    def log(self, **kwargs):
        """
        Function for storing the new values of the given attribute
        :param **kwargs:
        :return:
        """
        for key, value in kwargs.items():
            self.__dict__[key].log(value)

    def save(self, *args):
        """
        Saves the temporal statistics into a .hdf5 file
        :param **kwargs:
        :return:
        """
        with h5py.File(join(self.data_dir, 'time_log_' + self.timestamp + '.hdf5'), 'w') as f:
            for arg in args:
                self.__dict__[arg].save(f.create_group(arg))

    def load(self, filename, decimate_step=100):
        """
        Loads the temporal statistics and fills the attributes of the class
        :param decimate_step:
        :param filename: name of the .hdf5 file to load
        :return:
        """
        if not filename.endswith('.hdf5'):
            filename = filename + '.hdf5'

        with h5py.File(join(self.data_dir, filename), 'r') as f:
            for key, value in self.__dict__.items():
                if isinstance(value, LogData):
                    value.load(f[key], decimate_step)

    def plot_mean_min_max(self, *args):
        fig, ax, _ = print_init(False)
        for arg in args:
            # breakpoint()
            if arg in self.__dict__.keys():  # and isinstance(self.__dict__[arg], LogData):
                self.__dict__[arg].plot_mean_min_max(arg)
        plt.title("Mean and min-max statistics")

    def plot_mean_std(self, *args):
        fig, ax, _ = print_init(False)
        for arg in args:
            if arg in self.__dict__.keys():
                self.__dict__[arg].plot_mean_std(arg)

        plt.title("Mean and standard deviation statistics")


class EnvLogger(object):

    def __init__(self, env_name, log_dir, decimate_step=250) -> None:
        super().__init__()
        self.env_name = env_name
        self.log_dir = log_dir
        self.decimate_step = decimate_step
        self.data_dir = join(self.log_dir, self.env_name)
        self.fig_dir = self.base_dir = join(dirname(dirname(abspath(__file__))), join("figures", self.env_name))
        make_dir(self.fig_dir)

        self.params_df = pd.read_csv(join(self.data_dir, "params.tsv"), "\t")

        self.logs = {}

        mean_reward = []
        mean_feat_std = []
        mean_proxy = []

        # load trainings
        for timestamp in self.params_df.timestamp:
            self.logs[timestamp] = TemporalLogger(self.env_name, timestamp, self.log_dir, *["rewards", "features"])
            self.logs[timestamp].load(join(self.data_dir, f"time_log_{timestamp}"), self.decimate_step)

            # calculate statistics
            mean_reward.append(self.logs[timestamp].__dict__["rewards"].mean.mean())
            mean_feat_std.append(self.logs[timestamp].__dict__["features"].std.mean())
            mean_proxy.append(mean_reward[-1] * mean_feat_std[-1])

        # append statistics to df
        self.params_df["mean_reward"] = pd.Series(mean_reward, index=self.params_df.index)
        self.params_df["mean_feat_std"] = pd.Series(mean_feat_std, index=self.params_df.index)
        self.params_df["mean_proxy"] = pd.Series(mean_proxy, index=self.params_df.index)

    def plot_mean_std(self, *args):
        for key, val in self.logs.items():
            print(key)
            val.plot_mean_std(*args)

    def plot_proxy(self, window=1000):
        fig, ax, _ = print_init(False)
        for idx, (key, val) in enumerate(self.logs.items()):
            print(f'key={key}, proxy_val={self.params_df[self.params_df.timestamp == key]["mean_proxy"][idx]}')
            plt.plot(numpy_ewma_vectorized_v2(val.__dict__["features"].std, window) * numpy_ewma_vectorized_v2(
                val.__dict__["rewards"].mean, window), label=key)

        plt.title("Proxy for the reward-exploration problem")
        plot_postprocess(fig, ax, "Proxy", " value for the reward-exploration problem", None)

    def plot_decorator(self, keyword="rewards", window=1000, std_scale=1, inset_start_x=int(2e6),
                       inset_end_x=int(2.5e6),
                       y_inset_std_scale=5, save=False, zoom=2.5, loc=4):

        def stat_ewma(val, keyword, window):
            feat = val.__dict__[keyword]
            if keyword == "rewards":
                feat_stat = feat.mean
            elif keyword == "features":
                feat_stat = feat.std

            return numpy_ewma_vectorized_v2(feat_stat, window)

        fig, ax, axins, loc1, loc2 = print_init(zoom=zoom, loc=loc)

        # precompute y inset limits
        stats_last = []
        stats_max = []
        for val in self.logs.values():
            ewma_stat = stat_ewma(val, keyword, window)
            stats_last.append(ewma_stat[-1])
            stats_max.append(ewma_stat.max())

        stats_max = np.array(stats_max)
        stats_last = np.array(stats_last)
        y_inset_mean = np.median(stats_last)
        y_inset_std = y_inset_std_scale * stats_last.std()

        # create data structure for storing proxy values
        perf_metrics = {}

        # plot
        print("---------------------------------------------------")
        for idx, (key, val) in enumerate(self.logs.items()):
            # shorthand for the variable
            instance = self.params_df[self.params_df.timestamp == key]
            # print(f'key={key}, mean_reward={instance["mean_reward"][idx]}')

            label = instance2label(instance)

            # plot the mean of the feature
            # breakpoint()
            ewma_stat = stat_ewma(val, keyword, window)  # calculate exp mean
            print(f'{label}, {keyword}, {ewma_stat.max()}, {ewma_stat.max() / stats_max.max()}')
            perf_metrics[label] = 100 * ewma_stat.max() / stats_max.max()
            x_points = self.decimate_step * np.arange(
                ewma_stat.shape[0])  # placeholder for the x points (for xtick conversion)
            ax.plot(x_points, ewma_stat, label=label, color=color4label(label))

            if keyword == "rewards":
                # plot standard deviation (uncertainty)
                ewma_std = numpy_ewma_vectorized_v2(val.__dict__[keyword].std, window)
                ax.fill_between(x_points, ewma_stat + std_scale * ewma_std,
                                ewma_stat - std_scale * ewma_std, alpha=.2, color=color4label(label))

            # inset
            axins.plot(x_points, ewma_stat, label=label, color=color4label(label))
            axins.set_xlim(inset_start_x, inset_end_x)  # apply the x-limits
            axins.set_ylim(y_inset_mean - y_inset_std, y_inset_mean + y_inset_std)  # apply the y-limits
            mark_inset(ax, axins, loc1=loc1, loc2=loc2, fc="none", ec="0.5")

        plot_postprocess(fig, ax, keyword, self.env_name, self.fig_dir, save=save)

        return perf_metrics


class Renderer(object):

    def __init__(self, env, variant, log_dir) -> None:
        super().__init__()
        # sanity check
        if variant not in [0, 4]:
            raise ValueError(f"Invalid variant, got {variant}, should be 0 or 4")
        self.env_name = f"{env.capitalize()}NoFrameskip-v{variant}"
        self.log_dir = log_dir
        self.data_dir = join(self.log_dir, self.env_name)
        self.render_dir = join(dirname(dirname(abspath(__file__))), join("gifs", self.env_name))
        make_dir(self.render_dir)

        self.params_df = pd.read_csv(join(self.data_dir, "params.tsv"), "\t")

    def render(self, steps=2500, seed=42):
        for timestamp in self.params_df.timestamp:
            # query parameters
            instance = self.params_df[self.params_df.timestamp == timestamp]
            print(instance["n_stack"])
            n_stack = series_indexer(instance["n_stack"])

            attn_target_enum = label_enum_converter(series_indexer(instance['attention_target']))
            attn_type_enum = label_enum_converter(series_indexer(instance['attention_type']))

            # filenames for loading
            log_points = (.25, .5, .75, .99)
            files2load = [f"agent_best_loss_{timestamp}", f"agent_best_reward_{timestamp}",
                          *[f"agent_step_{i}_{timestamp}" for i in log_points]]

            # name conversion for GIF save
            label = instance2label(instance)
            gif_name = label.lower()
            gif_name = gif_name.replace(", ", "_")
            gif_name = gif_name.replace(" ", "_")
            files2save = [f"{gif_name}_best_loss", f"{gif_name}_best_reward",
                          *[f"{gif_name}_step_{i}" for i in log_points]]

            # iterate and render
            for agent_name, gif_name in zip(files2load, files2save):
                # make environment
                env = make_atari_env(self.env_name, num_env=1, seed=seed)
                env = VecFrameStack(env, n_stack=n_stack)

                # create agent
                print(agent_name, gif_name)
                agent = ICMAgent(n_stack, 1, env.action_space.n, attn_target_enum, attn_type_enum)

                self.load_and_eval(agent, env, agent_name, gif_name, steps)

    def load_and_eval(self, agent: ICMAgent, env, agent_path, gif_path, steps=2500):
        # load agent and set to evaluation mode
        agent.load_state_dict(torch.load(join(self.data_dir, agent_path)))
        agent.eval()

        # loop and acquire images
        images = []
        obs = env.reset()
        for _ in range(steps):
            tensor = torch.from_numpy(obs.transpose((0, 3, 1, 2))).float() / 255.
            tensor = tensor.cuda() if torch.cuda.is_available() else tensor
            action, _, _, _, _ = agent.a2c.get_action(tensor)
            _, _, _, _ = env.step(action)
            images.append(env.render(mode="rgb_array"))

        # render
        imageio.mimsave(join(self.render_dir, f"{gif_path}.gif"),
                        [np.array(img) for i, img in enumerate(images) if i % 2 == 0], fps=29)
