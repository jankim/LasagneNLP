__author__ = 'max'

import time
import sys
import argparse
from lasagne_nlp.utils import utils
import lasagne_nlp.utils.data_processor as data_processor
import theano.tensor as T
import theano
from lasagne_nlp.networks.networks import *
import lasagne.nonlinearities as nonlinearities


def main():
    parser = argparse.ArgumentParser(description='Tuning with bi-directional RNN')
    parser.add_argument('--fine_tune', action='store_true', help='Fine tune the word embeddings')
    parser.add_argument('--embedding', choices=['word2vec', 'senna'], help='Embedding for words', required=True)
    parser.add_argument('--embedding_dict', default='data/word2vec/GoogleNews-vectors-negative300.bin',
                        help='path for embedding dict')
    parser.add_argument('--batch_size', default=10, help='Number of sentences in each batch')
    parser.add_argument('--num_units', default=100, help='Number of hidden units in RNN')
    parser.add_argument('--oov', choices=['random', 'embedding'], help='Embedding for oov word', required=True)
    parser.add_argument('--regular', choices=['none', 'l2', 'dropout'], help='regularization for training',
                        required=True)
    parser.add_argument('--train')  # "data/POS-penn/wsj/split1/wsj1.train.original"
    parser.add_argument('--dev')  # "data/POS-penn/wsj/split1/wsj1.dev.original"
    parser.add_argument('--test')  # "data/POS-penn/wsj/split1/wsj1.test.original"

    args = parser.parse_args()

    def construct_input_layer(input_var, fine_tune, embedd_table, max_length, embedd_dim):
        if fine_tune:
            alphabet_size, _ = embedd_table.shape
            layer_input = lasagne.layers.InputLayer(shape=(None, max_length), input_var=input_var, name='input')
            layer_embedding = lasagne.layers.EmbeddingLayer(layer_input, input_size=alphabet_size,
                                                            output_size=embedd_dim,
                                                            W=embedd_table, name='embedding')
            return layer_embedding
        else:
            layer_input = lasagne.layers.InputLayer(shape=(None, max_length, embedd_dim), input_var=input_var,
                                                    name='input')
            return layer_input

    logger = utils.get_logger("BiRNN")
    fine_tune = args.fine_tune
    oov = args.oov
    regular = args.regular
    embedding = args.embedding
    embedding_path = args.embedding_dict
    train_path = args.train
    dev_path = args.dev
    test_path = args.test

    X_train, Y_train, mask_train, X_dev, Y_dev, mask_dev, X_test, Y_test, mask_test, \
    embedd_table, num_labels = data_processor.load_dataset_sequence_labeling(train_path, dev_path, test_path, oov=oov,
                                                                             fine_tune=fine_tune, embedding=embedding,
                                                                             embedding_path=embedding_path)

    logger.info("constructing network...")
    # create variables
    target_var = T.imatrix(name='targets')
    mask_var = T.matrix(name='masks', dtype=theano.config.floatX)
    if fine_tune:
        input_var = T.imatrix(name='inputs')
        num_data, max_length = X_train.shape
        _, embedd_dim = embedd_table.shape
    else:
        input_var = T.tensor3(name='inputs', dtype=theano.config.floatX)
        num_data, max_length, embedd_dim = X_train.shape

    # construct input and mask layers
    layer_input = construct_input_layer(input_var, fine_tune, embedd_table, max_length, embedd_dim)
    layer_mask = lasagne.layers.InputLayer(shape=(None, max_length), input_var=mask_var, name='mask')

    # construct bi-rnn
    num_units = args.num_units
    bi_rnn = build_BiRNN(layer_input, num_units, mask=layer_mask)

    # reshape bi-rnn to [batch * max_length, embedd_dim]
    bi_rnn = lasagne.layers.reshape(bi_rnn, (-1, [2]))

    # drop out layer?
    if regular == 'dropout':
        bi_rnn = lasagne.layers.DropoutLayer(bi_rnn, p=0.5)

    # construct output layer (dense layer with softmax)
    layer_output = lasagne.layers.DenseLayer(bi_rnn, num_units=num_labels, nonlinearity=nonlinearities.softmax)

    # get output of bi-rnn shape=[batch * max_length, #label]
    prediction_train = lasagne.layers.get_output(layer_output)
    prediction_eval = lasagne.layers.get_output(layer_output, deterministic=True)
    # flat target_var to vector
    target_var_flatten = target_var.flatten()
    # flat mask_var to vector
    mask_var_flatten = mask_var.flatten()

    # compute loss
    num_loss = mask_var_flatten.sum(dtype=theano.config.floatX)
    # for training, we use mean of loss over number of labels
    loss_train = lasagne.objectives.categorical_crossentropy(prediction_train, target_var_flatten)
    loss_train = (loss_train * mask_var_flatten).sum(dtype=theano.config.floatX) / num_loss
    # l2 regularization?
    if regular == 'l2':
        gamma = 1e-4
        l2_penalty = lasagne.regularization.regularize_network_params(layer_output, lasagne.regularization.l2)
        loss_train = loss_train + gamma * l2_penalty

    loss_eval = lasagne.objectives.categorical_crossentropy(prediction_eval, target_var_flatten)
    loss_eval = (loss_eval * mask_var_flatten).sum(dtype=theano.config.floatX) / num_loss

    # compute number of correct labels
    corr_train = lasagne.objectives.categorical_accuracy(prediction_train, target_var_flatten)
    corr_train = (corr_train * mask_var_flatten).sum(dtype=theano.config.floatX)

    corr_eval = lasagne.objectives.categorical_accuracy(prediction_eval, target_var_flatten)
    corr_eval = (corr_eval * mask_var_flatten).sum(dtype=theano.config.floatX)

    # Create update expressions for training.
    # hyper parameters to tune: learning rate, momentum, regularization.
    batch_size = args.batch_size
    learning_rate = 0.1
    decay_rate = 0.1
    momentum = 0.9
    params = lasagne.layers.get_all_params(layer_output, trainable=True)
    updates = lasagne.updates.sgd(loss_train, params=params, learning_rate=learning_rate)

    # Compile a function performing a training step on a mini-batch
    train_fn = theano.function([input_var, target_var, mask_var], [loss_train, corr_train, num_loss], updates=updates)
    # Compile a second function evaluating the loss and accuracy of network
    eval_fn = theano.function([input_var, target_var, mask_var], [loss_eval, corr_eval, num_loss])

    # Finally, launch the training loop.
    logger.info("Start training (number of training data: %d..." % (num_data))
    num_batches = num_data / batch_size
    num_epochs = 1000
    best_loss = 1e+12
    stop_count = 0
    for epoch in range(1, num_epochs + 1):
        print 'Epoch %d: ' % epoch
        train_err = 0.0
        train_corr = 0.0
        train_total = 0
        start_time = time.time()
        num_back = 0
        train_batches = 0
        for batch in utils.iterate_minibatches(X_train, Y_train, mask_train, batch_size, shuffle=True):
            inputs, targets, masks = batch
            err, corr, num = train_fn(inputs, targets, masks)
            train_err += err * num
            train_corr += corr
            train_total += num
            train_batches += 1
            time_ave = (time.time() - start_time) / train_batches
            time_left = (num_batches - train_batches) * time_ave

            # update log
            sys.stdout.write("\b" * num_back)
            log_info = 'train: %d/%d loss: %.4f, acc: %.2f%%, time left (estimated): %.2fs' % (
                min(train_batches * batch_size, num_data), num_data,
                train_err / train_total, train_corr * 100 / train_total, time_left)
            sys.stdout.write(log_info)
            num_back = len(log_info)

        # evaluate performance on dev data
        dev_err = 0.0
        dev_corr = 0.0
        dev_total = 0
        for batch in utils.iterate_minibatches(X_dev, Y_dev, mask_dev, batch_size):
            inputs, targets, masks = batch
            err, corr, num = eval_fn(inputs, targets, masks)
            dev_err += err * num
            dev_corr += corr
            dev_total += num
        print '\ndev loss: %.4f, corr: %d, total: %d, acc: %.2f%%' % (
            dev_err / dev_total, dev_corr, dev_total, dev_corr * 100 / dev_total)

        if best_loss < dev_err:
            stop_count += 1
        else:
            best_loss = dev_err
            stop_count = 0

        # stop if dev acc decrease 3 time straightly.
        if stop_count == 3:
            break

        # re-compile a function with new learning rate for training
        lr = learning_rate / (1.0 + epoch * decay_rate)
        updates = lasagne.updates.sgd(loss_train, params=params, learning_rate=lr)
        train_fn = theano.function([input_var, target_var, mask_var], [loss_train, corr_train, num_loss],
                                   updates=updates)

    # evaluate on test data.
    logger.info("evaluating test performance...")
    test_err = 0.0
    test_corr = 0.0
    test_total = 0
    for batch in utils.iterate_minibatches(X_test, Y_test, mask_test, batch_size):
        inputs, targets, masks = batch
        err, corr, num = eval_fn(inputs, targets, masks)
        test_err += err * num
        test_corr += corr
        test_total += num
    print 'test loss: %.4f, corr: %d, total: %d, acc: %.2f%%' % (
        test_err / test_total, test_corr, test_total, test_corr * 100 / test_total)


if __name__ == '__main__':
    main()