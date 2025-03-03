"""
Entry point for training and evaluating a dependency parser.

This implementation combines a deep biaffine graph-based parser with linearization and distance features.
For details please refer to paper: https://nlp.stanford.edu/pubs/qi2018universal.pdf.
"""

"""
Training and evaluation for the parser.
"""

import sys
import os
import shutil
import time
import argparse
import logging
import numpy as np
import random
import torch
from torch import nn, optim

import stanza.models.depparse.data as data
from stanza.models.depparse.data import DataLoader
from stanza.models.depparse.trainer import Trainer
from stanza.models.depparse import scorer
from stanza.models.common import utils
from stanza.models.common import pretrain
from stanza.models.common.data import augment_punct
from stanza.models.common.doc import *
from stanza.utils.conll import CoNLL
from stanza.models import _training_logging

logger = logging.getLogger('stanza')

def build_argparse():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_dir', type=str, default='data/depparse', help='Root dir for saving models.')
    parser.add_argument('--wordvec_dir', type=str, default='extern_data/word2vec', help='Directory of word vectors.')
    parser.add_argument('--wordvec_file', type=str, default=None, help='Word vectors filename.')
    parser.add_argument('--wordvec_pretrain_file', type=str, default=None, help='Exact name of the pretrain file to read')
    parser.add_argument('--train_file', type=str, default=None, help='Input file for data loader.')
    parser.add_argument('--eval_file', type=str, default=None, help='Input file for data loader.')
    parser.add_argument('--output_file', type=str, default=None, help='Output CoNLL-U file.')
    parser.add_argument('--gold_file', type=str, default=None, help='Output CoNLL-U file.')

    parser.add_argument('--mode', default='train', choices=['train', 'predict'])
    parser.add_argument('--lang', type=str, help='Language')
    parser.add_argument('--shorthand', type=str, help="Treebank shorthand")

    parser.add_argument('--hidden_dim', type=int, default=400)
    parser.add_argument('--char_hidden_dim', type=int, default=400)
    parser.add_argument('--deep_biaff_hidden_dim', type=int, default=400)
    parser.add_argument('--composite_deep_biaff_hidden_dim', type=int, default=100)
    parser.add_argument('--word_emb_dim', type=int, default=75)
    parser.add_argument('--char_emb_dim', type=int, default=100)
    parser.add_argument('--tag_emb_dim', type=int, default=50)
    parser.add_argument('--transformed_dim', type=int, default=125)
    parser.add_argument('--num_layers', type=int, default=3)
    parser.add_argument('--char_num_layers', type=int, default=1)
    parser.add_argument('--pretrain_max_vocab', type=int, default=250000)
    parser.add_argument('--word_dropout', type=float, default=0.33)
    parser.add_argument('--dropout', type=float, default=0.5)
    parser.add_argument('--rec_dropout', type=float, default=0, help="Recurrent dropout")
    parser.add_argument('--char_rec_dropout', type=float, default=0, help="Recurrent dropout")

    parser.add_argument('--no_char', dest='char', action='store_false', help="Turn off character model.")
    parser.add_argument('--charlm', action='store_true', help="Turn on contextualized char embedding using pretrained character-level language model.")
    parser.add_argument('--charlm_save_dir', type=str, default='saved_models/charlm', help="Root dir for pretrained character-level language model.")
    parser.add_argument('--charlm_shorthand', type=str, default=None, help="Shorthand for character-level language model training corpus.")
    parser.add_argument('--charlm_forward_file', type=str, default=None, help="Exact path to use for forward charlm")
    parser.add_argument('--charlm_backward_file', type=str, default=None, help="Exact path to use for backward charlm")

    parser.add_argument('--bert_model', type=str, default=None, help="Use an external bert model (requires the transformers package)")
    parser.add_argument('--no_bert_model', dest='bert_model', action="store_const", const=None, help="Don't use bert")
    parser.add_argument('--bert_hidden_layers', type=int, default=None, help="How many layers of hidden state to use from the transformer")
    parser.add_argument('--bert_finetune', default=False, action='store_true', help='Finetune the bert (or other transformer)')
    parser.add_argument('--no_bert_finetune', dest='bert_finetune', action='store_false', help="Don't finetune the bert (or other transformer)")
    parser.add_argument('--bert_learning_rate', default=1.0, type=float, help='Scale the learning rate for transformer finetuning by this much')

    parser.add_argument('--no_pretrain', dest='pretrain', action='store_false', help="Turn off pretrained embeddings.")
    parser.add_argument('--no_linearization', dest='linearization', action='store_false', help="Turn off linearization term.")
    parser.add_argument('--no_distance', dest='distance', action='store_false', help="Turn off distance term.")

    parser.add_argument('--sample_train', type=float, default=1.0, help='Subsample training data.')
    parser.add_argument('--optim', type=str, default='adam', help='sgd, adagrad, adam or adamax.')
    parser.add_argument('--lr', type=float, default=3e-3, help='Learning rate')
    parser.add_argument('--beta2', type=float, default=0.95)

    parser.add_argument('--max_steps', type=int, default=50000)
    parser.add_argument('--eval_interval', type=int, default=100)
    parser.add_argument('--max_steps_before_stop', type=int, default=3000)
    parser.add_argument('--batch_size', type=int, default=5000)
    parser.add_argument('--max_grad_norm', type=float, default=1.0, help='Gradient clipping.')
    parser.add_argument('--log_step', type=int, default=20, help='Print log every k steps.')
    parser.add_argument('--save_dir', type=str, default='saved_models/depparse', help='Root dir for saving models.')
    parser.add_argument('--save_name', type=str, default="{shorthand}_{embedding}_parser.pt", help="File name to save the model")

    parser.add_argument('--seed', type=int, default=1234)
    utils.add_device_args(parser)

    parser.add_argument('--augment_nopunct', type=float, default=None, help='Augment the training data by copying this fraction of punct-ending sentences as non-punct.  Default of None will aim for roughly 10%%')

    parser.add_argument('--wandb', action='store_true', help='Start a wandb session and write the results of training.  Only applies to training.  Use --wandb_name instead to specify a name')
    parser.add_argument('--wandb_name', default=None, help='Name of a wandb session to start when training.  Will default to the dataset short name')
    return parser

def parse_args(args=None):
    parser = build_argparse()
    args = parser.parse_args(args=args)

    if args.wandb_name:
        args.wandb = True

    args = vars(args)
    return args

def main(args=None):
    args = parse_args(args=args)

    utils.set_random_seed(args['seed'])

    logger.info("Running parser in {} mode".format(args['mode']))

    if args['mode'] == 'train':
        train(args)
    else:
        evaluate(args)

# TODO: refactor with tagger
def model_file_name(args):
    embedding = utils.embedding_name(args)
    model_file = args['save_name'].format(shorthand=args['shorthand'],
                                          embedding=embedding)
    model_dir = os.path.split(model_file)[0]

    if not os.path.exists(os.path.join(args['save_dir'], model_file)) and os.path.exists(model_file):
        return model_file
    return os.path.join(args['save_dir'], model_file)

# TODO: refactor with everywhere
def load_pretrain(args):
    pt = None
    if args['pretrain']:
        pretrain_file = pretrain.find_pretrain_file(args['wordvec_pretrain_file'], args['save_dir'], args['shorthand'], args['lang'])
        if os.path.exists(pretrain_file):
            vec_file = None
        else:
            vec_file = args['wordvec_file'] if args['wordvec_file'] else utils.get_wordvec_file(args['wordvec_dir'], args['shorthand'])
        pt = pretrain.Pretrain(pretrain_file, vec_file, args['pretrain_max_vocab'])
    return pt

def train(args):
    model_file = model_file_name(args)
    utils.ensure_dir(os.path.split(model_file)[0])

    # load pretrained vectors if needed
    pretrain = load_pretrain(args)

    # TODO: refactor.  the exact same thing is done in the tagger
    if args['charlm']:
        if args['charlm_shorthand'] is None:
            raise ValueError("CharLM Shorthand is required for loading pretrained CharLM model...")
        logger.info('Using pretrained contextualized char embedding')
        if not args['charlm_forward_file']:
            args['charlm_forward_file'] = '{}/{}_forward_charlm.pt'.format(args['charlm_save_dir'], args['charlm_shorthand'])
        if not args['charlm_backward_file']:
            args['charlm_backward_file'] = '{}/{}_backward_charlm.pt'.format(args['charlm_save_dir'], args['charlm_shorthand'])

    # load data
    logger.info("Loading data with batch size {}...".format(args['batch_size']))
    train_data, _ = CoNLL.conll2dict(input_file=args['train_file'])
    # possibly augment the training data with some amount of fake data
    # based on the options chosen
    logger.info("Original data size: {}".format(len(train_data)))
    train_data.extend(augment_punct(train_data, args['augment_nopunct'],
                                    keep_original_sentences=False))
    logger.info("Augmented data size: {}".format(len(train_data)))
    train_doc = Document(train_data)
    train_batch = DataLoader(train_doc, args['batch_size'], args, pretrain, evaluation=False)
    vocab = train_batch.vocab
    dev_doc = CoNLL.conll2doc(input_file=args['eval_file'])
    dev_batch = DataLoader(dev_doc, args['batch_size'], args, pretrain, vocab=vocab, evaluation=True, sort_during_eval=True)

    # pred and gold path
    system_pred_file = args['output_file']
    gold_file = args['gold_file']

    # skip training if the language does not have training or dev data
    if len(train_batch) == 0 or len(dev_batch) == 0:
        logger.info("Skip training because no data available...")
        sys.exit(0)

    if args['wandb']:
        import wandb
        wandb_name = args['wandb_name'] if args['wandb_name'] else "%s_depparse" % args['shorthand']
        wandb.init(name=wandb_name, config=args)
        wandb.run.define_metric('train_loss', summary='min')
        wandb.run.define_metric('dev_score', summary='max')

    logger.info("Training parser...")
    trainer = Trainer(args=args, vocab=vocab, pretrain=pretrain, device=args['device'])

    global_step = 0
    max_steps = args['max_steps']
    dev_score_history = []
    best_dev_preds = []
    current_lr = args['lr']
    global_start_time = time.time()
    format_str = 'Finished STEP {}/{}, loss = {:.6f} ({:.3f} sec/batch), lr: {:.6f}'

    using_amsgrad = False
    last_best_step = 0
    # start training
    train_loss = 0
    while True:
        do_break = False
        for i, batch in enumerate(train_batch):
            start_time = time.time()
            global_step += 1
            loss = trainer.update(batch, eval=False) # update step
            train_loss += loss
            if global_step % args['log_step'] == 0:
                duration = time.time() - start_time
                logger.info(format_str.format(global_step, max_steps, loss, duration, current_lr))

            if global_step % args['eval_interval'] == 0:
                # eval on dev
                logger.info("Evaluating on dev set...")
                dev_preds = []
                for batch in dev_batch:
                    preds = trainer.predict(batch)
                    dev_preds += preds
                dev_preds = utils.unsort(dev_preds, dev_batch.data_orig_idx)

                dev_batch.doc.set([HEAD, DEPREL], [y for x in dev_preds for y in x])
                CoNLL.write_doc2conll(dev_batch.doc, system_pred_file)
                _, _, dev_score = scorer.score(system_pred_file, gold_file)

                train_loss = train_loss / args['eval_interval'] # avg loss per batch
                logger.info("step {}: train_loss = {:.6f}, dev_score = {:.4f}".format(global_step, train_loss, dev_score))

                if args['wandb']:
                    wandb.log({'train_loss': train_loss, 'dev_score': dev_score})

                train_loss = 0

                # save best model
                if len(dev_score_history) == 0 or dev_score > max(dev_score_history):
                    last_best_step = global_step
                    trainer.save(model_file)
                    logger.info("new best model saved.")
                    best_dev_preds = dev_preds

                dev_score_history += [dev_score]

            if global_step - last_best_step >= args['max_steps_before_stop']:
                if not using_amsgrad:
                    logger.info("Switching to AMSGrad")
                    last_best_step = global_step
                    using_amsgrad = True
                    trainer.optimizer = optim.Adam(trainer.model.parameters(), amsgrad=True, lr=args['lr'], betas=(.9, args['beta2']), eps=1e-6)
                else:
                    do_break = True
                    break

            if global_step >= args['max_steps']:
                do_break = True
                break

        if do_break: break

        train_batch.reshuffle()

    logger.info("Training ended with {} steps.".format(global_step))

    if args['wandb']:
        wandb.finish()

    if len(dev_score_history) > 0:
        best_f, best_eval = max(dev_score_history)*100, np.argmax(dev_score_history)+1
        logger.info("Best dev F1 = {:.2f}, at iteration = {}".format(best_f, best_eval * args['eval_interval']))
    else:
        logger.info("Dev set never evaluated.  Saving final model.")
        trainer.save(model_file)


def evaluate(args):
    # file paths
    system_pred_file = args['output_file']
    gold_file = args['gold_file']

    model_file = model_file_name(args)
    # load pretrained vectors if needed
    pretrain = load_pretrain(args)

    load_args = {'charlm_forward_file': args.get('charlm_forward_file', None),
                 'charlm_backward_file': args.get('charlm_backward_file', None)}

    # load model
    logger.info("Loading model from: {}".format(model_file))
    trainer = Trainer(pretrain=pretrain, model_file=model_file, device=args['device'], args=load_args)
    loaded_args, vocab = trainer.args, trainer.vocab

    # load config
    for k in args:
        if k.endswith('_dir') or k.endswith('_file') or k in ['shorthand'] or k == 'mode':
            loaded_args[k] = args[k]

    # load data
    logger.info("Loading data with batch size {}...".format(args['batch_size']))
    doc = CoNLL.conll2doc(input_file=args['eval_file'])
    batch = DataLoader(doc, args['batch_size'], loaded_args, pretrain, vocab=vocab, evaluation=True, sort_during_eval=True)

    if len(batch) > 0:
        logger.info("Start evaluation...")
        preds = []
        for i, b in enumerate(batch):
            preds += trainer.predict(b)
    else:
        # skip eval if dev data does not exist
        preds = []
    preds = utils.unsort(preds, batch.data_orig_idx)

    # write to file and score
    batch.doc.set([HEAD, DEPREL], [y for x in preds for y in x])
    CoNLL.write_doc2conll(batch.doc, system_pred_file)

    if gold_file is not None:
        gold_doc = CoNLL.conll2doc(input_file=gold_file)

        scorer.score_named_dependencies(batch.doc, gold_doc)
        _, _, score = scorer.score(system_pred_file, gold_file)

        logger.info("Parser score:")
        logger.info("{} {:.2f}".format(args['shorthand'], score*100))

if __name__ == '__main__':
    main()
