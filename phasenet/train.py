import numpy as np
import tensorflow as tf
tf.compat.v1.disable_eager_execution()
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)
import argparse, os, time, logging
from tqdm import tqdm
import pandas as pd
import multiprocessing
from functools import partial
import pickle
from model import UNet, ModelConfig
from data_reader import DataReader_train, DataReader_test
from postprocess import extract_picks, save_picks, save_picks_json, extract_amplitude
from util import EMA, LMA

def read_args():

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="train", help="train/train_valid/test/debug")
    parser.add_argument("--epochs", default=100, type=int, help="number of epochs (default: 10)")
    parser.add_argument("--batch_size", default=20, type=int, help="batch size")
    parser.add_argument("--learning_rate", default=0.01, type=float, help="learning rate")
    parser.add_argument("--drop_rate", default=0.0, type=float, help="dropout rate")
    parser.add_argument("--decay_step", default=-1, type=int, help="decay step")
    parser.add_argument("--decay_rate", default=0.9, type=float, help="decay rate")
    parser.add_argument("--momentum", default=0.9, type=float, help="momentum")
    parser.add_argument("--optimizer", default="adam", help="optimizer: adam, momentum")
    parser.add_argument("--summary", default=True, type=bool, help="summary")
    parser.add_argument("--class_weights", nargs="+", default=[1, 1, 1], type=float, help="class weights")
    parser.add_argument("--model_dir", default="models", help="Checkpoint directory (default: None)")
    parser.add_argument("--load_model", action="store_true", help="Load checkpoint")
    parser.add_argument("--log_dir", default="log", help="Log directory (default: log)")
    parser.add_argument("--num_plots", default=10, type=int, help="Plotting training results")
    parser.add_argument("--min_p_prob", default=0.3, type=float, help="Probability threshold for P pick")
    parser.add_argument("--min_s_prob", default=0.3, type=float, help="Probability threshold for S pick")
    parser.add_argument("--format", default="numpy", help="Input data format")
    parser.add_argument("--train_dir", default="./dataset/waveform_train/", help="Input file directory")
    parser.add_argument("--train_list", default="./dataset/waveform.csv", help="Input csv file")
    parser.add_argument("--valid_dir", default=None, help="Input file directory")
    parser.add_argument("--valid_list", default=None, help="Input csv file")
    parser.add_argument("--test_dir", default=None, help="Input file directory")
    parser.add_argument("--test_list", default=None, help="Input csv file")
    parser.add_argument("--plot_figure", action="store_true", help="If plot figure for test")
    parser.add_argument("--save_prob", action="store_true", help="If save result for test")
    args = parser.parse_args()

    return args


def train_fn(args, data_reader, data_reader_valid=None):
    current_time = time.strftime("%y%m%d-%H%M%S")
    log_dir = os.path.join(args.log_dir, current_time)
    logging.info("Training log: {}".format(log_dir))
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    figure_dir = os.path.join(log_dir, 'figures')
    if not os.path.exists(figure_dir):
        os.makedirs(figure_dir)
        
    config = ModelConfig(X_shape=data_reader.X_shape, Y_shape=data_reader.Y_shape)
    if args.decay_step == -1:
        args.decay_step = data_reader.num_data // args.batch_size
    config.update_args(args)
    with open(os.path.join(log_dir, 'config.log'), 'w') as fp:
        fp.write('\n'.join("%s: %s" % item for item in vars(config).items()))

    with tf.compat.v1.name_scope('Input_Batch'):
        dataset = data_reader.dataset(args.batch_size, shuffle=True).repeat()
        batch = tf.compat.v1.data.make_one_shot_iterator(dataset).get_next()
        if data_reader_valid is not None:
            dataset_valid = data_reader_valid.dataset(args.batch_size, shuffle=False).repeat()
            valid_batch = tf.compat.v1.data.make_one_shot_iterator(dataset_valid).get_next()

    model = UNet(config, input_batch=batch)
    sess_config = tf.compat.v1.ConfigProto()
    sess_config.gpu_options.allow_growth = True
    # sess_config.log_device_placement = False
    
    with tf.compat.v1.Session(config=sess_config) as sess:

        summary_writer = tf.compat.v1.summary.FileWriter(log_dir, sess.graph)
        saver = tf.compat.v1.train.Saver(tf.compat.v1.global_variables(), max_to_keep=5)
        init = tf.compat.v1.global_variables_initializer()
        sess.run(init)

        if (args.model_dir is not None) and args.load_model:
            logging.info("restoring models...")
            latest_check_point = tf.train.latest_checkpoint(args.model_dir)
            saver.restore(sess, latest_check_point)

        flog = open(os.path.join(log_dir, 'loss.log'), 'w')
        train_loss = EMA(0.9)
        best_valid_loss = np.inf
        for epoch in range(args.epochs):
            progressbar = tqdm(range(0, data_reader.num_data, args.batch_size), desc="{}: epoch {}".format(log_dir.split("/")[-1], epoch))
            for _ in progressbar:
                loss_batch, _, _ = sess.run([model.loss, model.train_op, model.global_step], 
                                            feed_dict={model.drop_rate: args.drop_rate, model.is_training: True})
                train_loss(loss_batch)
                progressbar.set_description("{}: epoch {}, loss={:.6f}, mean={:.6f}".format(log_dir.split("/")[-1], epoch, loss_batch, train_loss.value))
            flog.write("epoch: {}, mean loss: {}\n".format(epoch, train_loss))
            
            if data_reader_valid is not None:
                valid_loss = LMA()
                progressbar = tqdm(range(0, data_reader_valid.num_data, args.batch_size), desc="Valid:")
                for _ in progressbar:
                    loss_batch, X_batch, Y_batch, fname_batch = sess.run([model.loss, valid_batch[0], valid_batch[1], valid_batch[2]], 
                                                                         feed_dict={model.drop_rate: 0, model.is_training: False})
                    valid_loss(loss_batch)
                    progressbar.set_description("valid, loss={:.6f}, mean={:.6f}".format(loss_batch, valid_loss.value))
                if valid_loss.value < best_valid_loss:
                    best_valid_loss = valid_loss.value
                    saver.save(sess, os.path.join(args.model_dir, "model_{}.ckpt".format(epoch)))
                flog.write("Valid: mean loss: {}\n".format(valid_loss.value))
            else:
                loss_batch, X_batch, Y_batch, fname_batch = sess.run([model.loss, batch[0], batch[1], batch[2]], 
                                                                      feed_dict={model.drop_rate: 0, model.is_training: False})
                saver.save(sess, os.path.join(args.model_dir, "model_{}.ckpt".format(epoch)))
            flog.flush()

            # try: ## IO Error on cluster
            #     flog.flush()
            #     pool.map(partial(plot_result_thread,
            #                                     pred = preds_batch,
            #                                     X = X_batch,
            #                                     Y = Y_batch,
            #                                     fname = ["{:03d}_{:03d}".format(epoch, x).encode() for x in range(args.num_plots)],
            #                                     figure_dir = figure_dir),
            #                     range(args.num_plots))
            #     saver.save(sess, os.path.join(log_dir, "model_{}.ckpt".format(epoch)))
            # except:
            #     pass
        flog.close()

    return 0

def test_fn(args, data_reader, figure_dir=None, result_dir=None):
    current_time = time.strftime("%y%m%d-%H%M%S")
    logging.info("{} log: {}".format(args.mode, current_time))
    log_dir = os.path.join(args.log_dir, args.mode, current_time)
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    if (args.plot_figure == True ) and (figure_dir is None):
        figure_dir = os.path.join(log_dir, 'figures')
        if not os.path.exists(figure_dir):
            os.makedirs(figure_dir)
    if (args.save_prob == True) and (result_dir is None):
        result_dir = os.path.join(log_dir, 'results')
        if not os.path.exists(result_dir):
            os.makedirs(result_dir)

    config = ModelConfig(X_shape=data_reader.X_shape, Y_shape=data_reader.Y_shape)
    config.update_args(args)
    with open(os.path.join(log_dir, 'config.log'), 'w') as fp:
        fp.write('\n'.join("%s: %s" % item for item in vars(config).items()))

    with tf.compat.v1.name_scope('Input_Batch'):
        dataset = data_reader.dataset(args.batch_size, shuffle=False)
        batch = tf.compat.v1.data.make_one_shot_iterator(dataset).get_next()

    model = UNet(config, input_batch=batch, mode='test')
    sess_config = tf.compat.v1.ConfigProto()
    sess_config.gpu_options.allow_growth = True
    # sess_config.log_device_placement = False

    with tf.compat.v1.Session(config=sess_config) as sess:

        saver = tf.compat.v1.train.Saver(tf.compat.v1.global_variables())
        init = tf.compat.v1.global_variables_initializer()
        sess.run(init)

        logging.info("restoring models...")
        latest_check_point = tf.train.latest_checkpoint(args.model_dir)
        saver.restore(sess, latest_check_point)
        
        flog = open(os.path.join(log_dir, 'loss.log'), 'w')
        test_loss = LMA()
        progressbar = tqdm(range(0, data_reader.num_data, args.batch_size), desc=args.mode)
        for step in progressbar:
            loss_batch, X_batch, Y_batch, fname_batch, itp_batch, its_batch \
                = sess.run([model.loss, batch[0], batch[1], batch[2], batch[3], batch[4]], 
                           feed_dict={model.drop_rate: 0, model.is_training: False})

            test_loss(loss_batch)
            progressbar.set_description("{}, loss={:.6f}, mean loss={:6f}".format(args.mode, loss_batch, test_loss.value))

            # itp_batch = clean_queue(itp_batch)
            # its_batch = clean_queue(its_batch)
            # picks_batch = pool.map(partial(postprocessing_thread,
            #                                                  pred = pred_batch,
            #                                                  X = X_batch,
            #                                                  Y = Y_batch,
            #                                                  itp = itp_batch,
            #                                                  its = its_batch,
            #                                                  fname = fname_batch,
            #                                                  result_dir = result_dir,
            #                                                  figure_dir = figure_dir),
            #                                  range(len(pred_batch)))
            # picks.extend(picks_batch)
            # itp.extend(itp_batch)
            # its.extend(its_batch)

        flog.write("mean loss: {}\n".format(test_loss))
        # metrics_p, metrics_s = calculate_metrics(picks, itp, its, tol=0.1)
        # flog.write("P-phase: Precision={}, Recall={}, F1={}\n".format(metrics_p[0], metrics_p[1], metrics_p[2]))
        # flog.write("S-phase: Precision={}, Recall={}, F1={}\n".format(metrics_s[0], metrics_s[1], metrics_s[2]))
        flog.close()

    return 0

def main(args):

    logging.basicConfig(format='%(asctime)s %(message)s', level=logging.INFO)
    coord = tf.train.Coordinator()

    if (args.mode == "train") or (args.mode == "train_valid"):
        with tf.compat.v1.name_scope('create_inputs'):
            data_reader = DataReader_train(format=args.format,
                                           data_dir=args.train_dir,
                                           data_list=args.train_list)
            if args.mode == "train_valid":
                data_reader_valid = DataReader_train(format=args.format,
                                                     data_dir=args.valid_dir,
                                                     data_list=args.valid_list)
                logging.info("Dataset size: train {}, valid {}".format(data_reader.num_data, data_reader_valid.num_data))
            else:
                data_reader_valid = None
                logging.info("Dataset size: train {}".format(data_reader.num_data))
        train_fn(args, data_reader, data_reader_valid)
    
    elif args.mode == "test":
        with tf.compat.v1.name_scope('create_inputs'):
            data_reader = DataReader_test(format=args.format,
                                          data_dir=args.test_dir,
                                          data_list=args.test_list)
        test_fn(args, data_reader)

    else:
        print("mode should be: train, train_valid, or test")

    return


if __name__ == '__main__':
    args = read_args()
    main(args)
