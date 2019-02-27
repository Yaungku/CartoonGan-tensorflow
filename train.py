import os
import numpy as np
import tensorflow as tf
import matplotlib as mpl

mpl.use("agg")
import matplotlib.pyplot as plt
from glob import glob
from datetime import datetime
from generator import Generator
from fixed_vgg import FixedVGG
from discriminator import Discriminator


__no_tqdm__ = False
try:
    from tqdm import tqdm
except (ModuleNotFoundError, ImportError):
    __no_tqdm__ = True


def _tqdm(res, *args, **kwargs):
    return res


class Trainer:
    def __init__(
        self,
        dataset_name,
        source_domain,
        target_domain,
        input_size,
        batch_size,
        sample_size,
        show_progress,
        logger,
        logdir,
        save_dir,
        pass_vgg,
        pretrain_learning_rate,
        pretrain_num_steps,
        pretrain_reporting_steps,
        pretrain_generator_name,
        **kwargs,
    ):
        self.ascii = os.name == "nt"
        self.dataset_name = dataset_name
        self.source_domain = source_domain
        self.target_domain = target_domain
        self.input_size = input_size
        self.batch_size = batch_size
        self.sample_size = sample_size
        self.logdir = logdir
        self.save_dir = save_dir
        self.pass_vgg = pass_vgg
        self.pretrain_learning_rate = pretrain_learning_rate
        self.pretrain_num_steps = pretrain_num_steps
        self.pretrain_reporting_steps = pretrain_reporting_steps
        self.pretrain_generator_name = pretrain_generator_name

        if logger is not None:
            self.logger = logger
        else:
            import logging

            self.logger = logging.getLogger()
            self.logger.setLevel(logging.info)
            self.logger.warning(
                "You are using the root logger, which has bad a format."
            )
            self.logger.warning("Please consider passing a better logger.")

        if not show_progress or __no_tqdm__:
            self.tqdm = _tqdm
        else:
            self.tqdm = tqdm

    def _save_generated_images(self, batch_x, directory="result", image_name=None, num_images_per_row=8):
        batch_size = batch_x.shape[0]
        fig_width = 8
        num_rows = batch_size // num_images_per_row if batch_size >= num_images_per_row else 1
        # fig_height = num_rows * 7 if batch_size >= num_images_per_row else 7
        fig_height = 6

        fig = plt.figure(figsize=(fig_width, fig_height))
        for i in range(batch_size):
            fig.add_subplot(num_rows, num_images_per_row, i + 1)
            plt.imshow(batch_x[i])
            plt.axis("off")
        if image_name is not None:
            if not os.path.exists(directory):
                os.makedirs(directory)
            plt.savefig(os.path.join(directory, image_name))
        plt.close(fig)

    def get_dataset(self, dataset_name, domain, _type, batch_size):
        files = glob(os.path.join(
            "datasets", dataset_name, f"{_type}{domain}", "*"))
        self.logger.info(f"{len(files)} domain{domain} images available in {_type}{domain} folder.")

        ds = tf.data.Dataset.from_tensor_slices(files)

        def image_processing(filename):
            x = tf.read_file(filename)
            x = tf.image.decode_jpeg(x, channels=3)
            img = tf.image.resize_images(x, [self.input_size, self.input_size])
            img = tf.cast(img, tf.float32) / 127.5 - 1
            return img

        return ds.map(image_processing).shuffle(10000).repeat().batch(batch_size)

    def pretrain_generator(self):
        self.logger.info(
            f"Pretraining generator with {self.pretrain_num_steps} steps, batch size: {self.batch_size}..."
        )
        self.logger.info(
            f"Building {self.dataset_name} with domain {self.source_domain}..."
        )

        ds = self.get_dataset(self.dataset_name, self.source_domain, 'train', self.batch_size)
        ds_iter = ds.make_initializable_iterator()
        input_images = ds_iter.get_next()

        self.logger.info("Initializing generator...")
        g = Generator(input_size=None)
        generated_images = g(input_images)

        if self.pass_vgg:
            self.logger.info("Initializing VGG for computing content loss...")
            vgg = FixedVGG()
            input_content = vgg.build_graph(input_images)
            generated_content = vgg.build_graph(generated_images)
            content_loss = tf.reduce_mean(tf.abs(input_content - generated_content))
        else:
            self.logger.info("Defining content loss without VGG...")
            content_loss = tf.reduce_mean(tf.abs(input_images - generated_images))

        self.logger.info("Setting up optimizer to update generator's parameters...")
        opt = tf.train.AdamOptimizer(learning_rate=self.pretrain_learning_rate)
        train_op = opt.minimize(content_loss, var_list=g.to_save_vars)

        self.logger.info("Start training...")
        start = datetime.utcnow()
        batch_losses = []
        with tf.Session() as sess:
            sess.run(tf.global_variables_initializer())
            ds_iter.initializer.run()

            self.logger.info("Loading previous checkpoints...")
            try:
                g.load(sess, self.save_dir, self.pretrain_generator_name)
                self.logger.info(f"Successfully loaded {self.pretrain_generator_name}...")
            except ValueError:
                self.logger.info(f"{self.pretrain_generator_name} checkpoints not found, start from scratch...")

            self.logger.info(f"Sampling {self.sample_size} images for tracking generator's performance...")
            real_batches = []
            for _ in range(int(self.sample_size / self.batch_size)):
                real_batches.append(sess.run(input_images))

            self._save_generated_images(
                np.clip(np.concatenate(real_batches, axis=0), 0, 1),
                image_name='sample_images.png'
            )

            for step in range(self.pretrain_num_steps):
                _, batch_loss = sess.run([train_op, content_loss])
                batch_losses.append(batch_loss)

                if step and step % self.pretrain_reporting_steps == 0:
                    fake_batches = []
                    for real_batch in real_batches:
                        fake_batches.append(sess.run(generated_images, {input_images: real_batch}))

                    g.save(sess, self.save_dir, self.pretrain_generator_name)
                    self._save_generated_images(
                        np.clip(np.concatenate(fake_batches, axis=0), 0, 1),
                        image_name=f"generated_images_at_step_{step}.png"
                    )

                    self.logger.info(
                        "[Step {step}] batch_loss: {:.3f}, {} elapsed".format(batch_loss, datetime.utcnow() - start)
                    )

                    with open("result/batch_losses.tsv", "a") as f:
                        f.write(step, '\t', batch_loss + '\n')

    def train_gan(self, **kwargs):
        ckpt_name = 'generater_adv_training'

        self.logger.info("Building data sets for both source / target domains...")
        ds_a = self.get_dataset(self.dataset_name, self.source_domain, 'train', self.batch_size)
        ds_b = self.get_dataset(self.dataset_name, self.target_domain, 'train', self.batch_size)
        ds_b_smooth = self.get_dataset(self.dataset_name, self.target_domain + '_smooth', 'train', self.batch_size)

        ds_a_iter = ds_a.make_initializable_iterator()
        ds_b_iter = ds_b.make_initializable_iterator()
        ds_b_smooth_iter = ds_b_smooth.make_initializable_iterator()

        input_a = ds_a_iter.get_next()
        input_b = ds_b_iter.get_next()
        input_b_smooth = ds_b_smooth_iter.get_next()

        self.logger.info("Building generator...")
        g = Generator(input_size=self.input_size)
        generated_b = g(input_a)

        self.logger.info("Building discriminator...")
        d = Discriminator(input_size=self.input_size)
        d_real_out = d.build_graph(input_b)
        d_fake_out = d.build_graph(generated_b, reuse=True)
        d_smooth_out = d.build_graph(input_b_smooth, reuse=True)

        self.logger.info("Define content loss using VGG...")
        vgg = FixedVGG()
        v_real_out = vgg.build_graph(input_a)
        v_fake_out = vgg.build_graph(generated_b)
        content_loss = tf.reduce_mean(tf.abs(v_real_out - v_fake_out))

        self.logger.info("Define generator/discriminator losses...")
        d_real_loss = tf.reduce_mean(tf.losses.sigmoid_cross_entropy(tf.ones_like(d_real_out), d_real_out))
        d_fake_loss = tf.reduce_mean(tf.losses.sigmoid_cross_entropy(tf.zeros_like(d_fake_out), d_fake_out))
        d_smooth_loss = tf.reduce_mean(tf.losses.sigmoid_cross_entropy(tf.zeros_like(d_smooth_out), d_smooth_out))
        d_loss = d_real_loss + d_fake_loss + d_smooth_loss

        g_adversarial_loss = tf.reduce_mean(tf.losses.sigmoid_cross_entropy(tf.ones_like(d_fake_out), d_fake_out))
        g_loss = g_adversarial_loss + 10 * content_loss

        self.logger.info("Define optimizers...")
        g_optimizer = tf.train.AdamOptimizer(1e-4)
        g_train_op = g_optimizer.minimize(g_loss, var_list=g.to_save_vars)

        d_optimizer = tf.train.AdamOptimizer(1e-4)
        d_train_op = d_optimizer.minimize(d_loss, var_list=d.to_save_vars)

        start = datetime.utcnow()
        with tf.Session() as sess:

            sess.run(tf.global_variables_initializer())
            ds_a_iter.initializer.run()
            ds_b_iter.initializer.run()
            ds_b_smooth_iter.initializer.run()

            self.logger.info("Initializing generator using pre-trained weights...")
            g.load(sess, self.save_dir, 'pretrain_generator_with_vgg')
            # g.load(sess, self.save_dir, ckpt_name)  # TODO: use previously trained gan

            sample_size = 32
            self.logger.info(f"Pick {sample_size} input images for tracking generator's performance...")
            real_batches = []

            for _ in range(int(sample_size / self.batch_size)):
                real_batches.append(sess.run(input_a))

            self._save_generated_images(
                np.clip(np.concatenate(real_batches, axis=0), 0, 1),
                image_name='sampled_images.png'
            )

            num_steps = 5000
            for step in range(num_steps):

                # update D
                _, d_batch_loss = sess.run([d_train_op, d_loss])

                # update G
                _, g_batch_loss, g_content_loss, g_adv_loss = sess.run([g_train_op, g_loss, content_loss, g_adversarial_loss])

                reporting_steps = 100
                if step and step % reporting_steps == 0:
                    fake_batches = []
                    for real_batch in real_batches:
                        fake_batches.append(sess.run(generated_b, {input_a: real_batch}))

                    g.save(sess, self.save_dir, ckpt_name)
                    self._save_generated_images(
                        np.clip(np.concatenate(fake_batches, axis=0), 0, 1),
                        image_name=f"gan_images_at_step_{step}.png"
                    )

                    res = "Finish step {} with d_batch_loss: {:.2f}, g_batch_loss: {:.2f}, g_content_loss: {:.2f}, g_adv_loss: {:.2f}, time elapsed: {}"
                    self.logger.info(res.format(step, d_batch_loss, g_batch_loss, g_content_loss, g_adv_loss, datetime.utcnow() - start))


def main(**kwargs):
    t = Trainer(**kwargs)
    t.pretrain_generator()

    # t.train_gan(**kwargs)


if __name__ == "__main__":
    import argparse
    import sys
    import logging

    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset_name", type=str, default="realworld2cartoon")
    parser.add_argument("--input_size", type=int, default=256)
    parser.add_argument("--batch_size", type=int, default=1)
    parser.add_argument("--sample_size", type=int, default=32)
    parser.add_argument("--source_domain", type=str, default="A")
    parser.add_argument("--target_domain", type=str, default="B")
    parser.add_argument("--num_steps", type=int, default=600000)
    parser.add_argument("--reporting_steps", type=int, default=100)
    parser.add_argument("--pass_vgg", action="store_true")
    parser.add_argument("--pretrain_learning_rate", type=float, default=1e-5)
    parser.add_argument("--pretrain_num_steps", type=int, default=60000)
    parser.add_argument("--pretrain_reporting_steps", type=int, default=100)
    parser.add_argument("--logdir", type=str, default="runs")
    parser.add_argument("--save_dir", type=str, default="ckpts")
    parser.add_argument("--pretrain_generator_name", type=str, default="pretrain_generator")
    parser.add_argument(
        "--logging_lvl",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
    )
    parser.add_argument("--logger_out_file", type=str, default=None)
    parser.add_argument("--not_show_progress_bar", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--show_tf_cpp_log", action="store_true")

    args = parser.parse_args()

    if not args.show_tf_cpp_log:
        os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"

    args.show_progress = not args.not_show_progress_bar
    log_lvl = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "critical": logging.CRITICAL,
    }
    args.logger = logging.getLogger("Trainer")
    if args.debug:
        args.logger.setLevel(logging.DEBUG)
    else:
        args.logger.setLevel(log_lvl[args.logging_lvl])
    formatter = logging.Formatter(
        "[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    stdhandler = logging.StreamHandler(sys.stdout)
    stdhandler.setFormatter(formatter)
    args.logger.addHandler(stdhandler)
    if args.logger_out_file is not None:
        fhandler = logging.StreamHandler(open(args.logger_out_file, "a"))
        fhandler.setFormatter(formatter)
        args.logger.addHandler(fhandler)
    kwargs = vars(args)
    main(**kwargs)
