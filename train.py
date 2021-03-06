import os
import math
import json
import joblib
import random
import argparse
import numpy as np
import tensorflow as tf

from functools import partial
from sklearn.utils import shuffle
from sklearn.metrics import accuracy_score

from opt import adam, warmup_cosine, warmup_linear, warmup_constant
from datasets import rocstories
from analysis import rocstories as rocstories_analysis
from text_utils import TextEncoder
from utils import encode_dataset, iter_data, find_trainable_variables, get_ema_vars
from utils import convert_gradient_to_tensor, shape_list, ResultLogger, assign_to_gpu, average_grads, make_path


def gelu(x):
    return 0.5*x*(1+tf.tanh(math.sqrt(2/math.pi)*(x+0.044715*tf.pow(x, 3))))


def swish(x):
    return x*tf.nn.sigmoid(x)

act_fns = {
    'relu': tf.nn.relu,
    'swish': swish,
    'gelu': gelu
}

lr_schedules = {
    'warmup_cosine': warmup_cosine,
    'warmup_linear': warmup_linear,
    'warmup_constant': warmup_constant,
}


def _norm(x, g=None, b=None, e=1e-5, axis=[1]):
    u = tf.reduce_mean(x, axis=axis, keep_dims=True)
    s = tf.reduce_mean(tf.square(x-u), axis=axis, keep_dims=True)
    x = (x - u) * tf.rsqrt(s + e)
    if g is not None and b is not None:
        x = x*g + b
    return x


def norm(x, scope, axis=[-1]):
    with tf.variable_scope(scope):
        n_state = shape_list(x)[-1]
        g = tf.get_variable("g", [n_state], initializer=tf.constant_initializer(1))
        b = tf.get_variable("b", [n_state], initializer=tf.constant_initializer(0))
        g, b = get_ema_vars(g, b)
        return _norm(x, g, b, axis=axis)


def dropout(x, pdrop, train):
    if train and pdrop > 0:
        x = tf.nn.dropout(x, 1-pdrop)
    return x


def mask_attn_weights(w):
    n = shape_list(w)[-1]
    b = tf.matrix_band_part(tf.ones([n, n]), -1, 0)
    b = tf.reshape(b, [1, 1, n, n])
    w = w*b + -1e9*(1-b)
    return w


def split_states(x, n):
    x_shape = shape_list(x)
    m = x_shape[-1]
    new_x_shape = x_shape[:-1]+[n, m//n]
    return tf.reshape(x, new_x_shape)


def merge_states(x):
    x_shape = shape_list(x)
    new_x_shape = x_shape[:-2]+[np.prod(x_shape[-2:])]
    return tf.reshape(x, new_x_shape)


def split_heads(x, n, k=False):
    if k:
        return tf.transpose(split_states(x, n), [0, 2, 3, 1])
    else:
        return tf.transpose(split_states(x, n), [0, 2, 1, 3])


def merge_heads(x):
    return merge_states(tf.transpose(x, [0, 2, 1, 3]))


def conv1d(x, scope, nf, rf,
           w_init=tf.random_normal_initializer(stddev=0.02),
           b_init=tf.constant_initializer(0),
           pad='VALID',
           train=False):

    with tf.variable_scope(scope):
        nx = shape_list(x)[-1]
        w = tf.get_variable("w", [rf, nx, nf], initializer=w_init)
        b = tf.get_variable("b", [nf], initializer=b_init)
        if rf == 1: #faster 1x1 conv
            c = tf.reshape(tf.matmul(tf.reshape(x, [-1, nx]), tf.reshape(w, [-1, nf]))+b, shape_list(x)[:-1]+[nf])
        else: #was used to train LM
            c = tf.nn.conv1d(x, w, stride=1, padding=pad)+b
        return c


def clf(x, ny, w_init=tf.random_normal_initializer(stddev=0.02), b_init=tf.constant_initializer(0), train=False):
    with tf.variable_scope('clf'):
        nx = shape_list(x)[-1]
        w = tf.get_variable("w", [nx, ny], initializer=w_init)
        b = tf.get_variable("b", [ny], initializer=b_init)
        return tf.matmul(x, w)+b


argmax = lambda x: np.argmax(x, 1)

pred_fns = {
    'rocstories': argmax,
}

file_names = {
    'rocstories': 'ROCStories.tsv',
}

label_decoders = {
    'rocstories':None,
}


class Model(object):
    def __init__(self, params):

        self.params = params
        self.logger = ResultLogger(path=os.path.join(self.params["log_dir"],
                                                     '{}.jsonl'.format(self.params["desc"])),
                                   **args.__dict__)
        self.encoder = None
        self.max_len = None
        self.n_vocab = None
        self.clf_token = None
        self.n_updates_total = None
        self.best_score = 0
        self.n_special = 3
        self.n_updates = 0
        self.n_epochs = 0
        self.n_batch_train = 0
        self.sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True))

        self.X_train, self.M_train, self.Y_train = None, None, None
        self.X, self.M, self.Y = None, None, None
        self.n_train, self.n_valid = None, None
        self.trX, self.trM, self.vaX, self.vaM, self.teX, self.teM = None, None, None, None, None, None

        self.vaY = None
        self.trY = None

        self.eval_mgpu_logits, self.eval_mgpu_clf_losses, self.eval_mgpu_lm_losses = None, None, None
        self.eval_logits, self.eval_clf_losses, self.eval_lm_losses = None, None, None
        self.eval_clf_loss = None
        self.eval_mgpu_clf_loss = None

        random.seed(self.params["seed"])
        np.random.seed(self.params["seed"])
        tf.set_random_seed(self.params["seed"])

    def save(self, path, params):
        ps = self.sess.run(params)
        joblib.dump(ps, make_path(path))

    def log(self, params):
        tr_logits, tr_cost = self.iter_apply(self.trX[:self.n_valid], self.trM[:self.n_valid], self.trY[:self.n_valid])
        va_logits, va_cost = self.iter_apply(self.vaX, self.vaM, self.vaY)
        tr_cost = tr_cost / len(self.trY[:self.n_valid])
        va_cost = va_cost / self.n_valid
        tr_acc = accuracy_score(self.trY[:self.n_valid], np.argmax(tr_logits, 1)) * 100.
        va_acc = accuracy_score(self.vaY, np.argmax(va_logits, 1)) * 100.

        self.logger.log(n_epochs=self.n_epochs,
                        n_updates=self.n_updates,
                        tr_cost=tr_cost,
                        va_cost=va_cost,
                        tr_acc=tr_acc,
                        va_acc=va_acc)

        print('%d %d %.3f %.3f %.2f %.2f' % (self.n_epochs, self.n_updates, tr_cost, va_cost, tr_acc, va_acc))

        score = va_acc
        if score > self.best_score:
            self.best_score = score
            self.save(os.path.join(self.params["save_dir"], self.params["desc"], 'best_params.jl'), params)

    def _attn(self, q, k, v, train=False, scale=False):
        w = tf.matmul(q, k)

        if scale:
            n_state = shape_list(v)[-1]
            w = w * tf.rsqrt(tf.cast(n_state, tf.float32))

        w = mask_attn_weights(w)
        w = tf.nn.softmax(w)
        w = dropout(w, self.params["attn_pdrop"], train)
        a = tf.matmul(w, v)
        return a

    def attn(self, x, scope, n_state, n_head, train=False, scale=False):
        assert n_state % n_head == 0
        with tf.variable_scope(scope):
            c = conv1d(x, 'c_attn', n_state * 3, 1, train=train)
            q, k, v = tf.split(c, 3, 2)
            q = split_heads(q, n_head)
            k = split_heads(k, n_head, k=True)
            v = split_heads(v, n_head)
            a = self._attn(q, k, v, train=train, scale=scale)
            a = merge_heads(a)
            a = conv1d(a, 'c_proj', n_state, 1, train=train)
            a = dropout(a, self.params["resid_pdrop"], train)
            return a

    def mlp(self, x, scope, n_state, train=False):
        with tf.variable_scope(scope):
            nx = shape_list(x)[-1]
            act = act_fns[self.params["afn"]]
            h = act(conv1d(x, 'c_fc', n_state, 1, train=train))
            h2 = conv1d(h, 'c_proj', nx, 1, train=train)
            h2 = dropout(h2, self.params["resid_pdrop"], train)
            return h2

    def block(self, x, scope, train=False, scale=False):
        with tf.variable_scope(scope):
            nx = shape_list(x)[-1]
            a = self.attn(x, 'attn', nx, self.params["n_head"], train=train, scale=scale)
            n = norm(x + a, 'ln_1')
            m = self.mlp(n, 'mlp', nx * 4, train=train)
            h = norm(n + m, 'ln_2')
            return h

    def embed(self, X, we):
        we = convert_gradient_to_tensor(we)
        e = tf.gather(we, X)
        h = tf.reduce_sum(e, 2)
        return h

    def model(self, X, M, Y, train=False, reuse=False):
        with tf.variable_scope('model', reuse=reuse):
            we = tf.get_variable("we",
                                 [self.n_vocab + self.n_special + self.params["n_ctx"], self.params["n_embd"]],
                                 initializer=tf.random_normal_initializer(stddev=0.02))

            we = dropout(we, self.params["embd_pdrop"], train)

            X = tf.reshape(X, [-1, self.params["n_ctx"], 2])
            M = tf.reshape(M, [-1, self.params["n_ctx"]])

            h = self.embed(X, we)
            for layer in range(self.params["n_layer"]):
                h = self.block(h, 'h%d' % layer, train=train, scale=True)

            lm_h = tf.reshape(h[:, :-1], [-1, self.params["n_embd"]])
            lm_logits = tf.matmul(lm_h, we, transpose_b=True)
            lm_losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=lm_logits,
                                                                       labels=tf.reshape(X[:, 1:, 0], [-1]))
            lm_losses = tf.reshape(lm_losses, [shape_list(X)[0], shape_list(X)[1] - 1])
            lm_losses = tf.reduce_sum(lm_losses * M[:, 1:], 1) / tf.reduce_sum(M[:, 1:], 1)

            clf_h = tf.reshape(h, [-1, self.params["n_embd"]])
            pool_idx = tf.cast(tf.argmax(tf.cast(tf.equal(X[:, :, 0], self.clf_token), tf.float32), 1), tf.int32)
            clf_h = tf.gather(clf_h, tf.range(shape_list(X)[0], dtype=tf.int32) * self.params["n_ctx"] + pool_idx)

            clf_h = tf.reshape(clf_h, [-1, 2, self.params["n_embd"]])
            if train and self.params["clf_pdrop"] > 0:
                shape = shape_list(clf_h)
                shape[1] = 1
                clf_h = tf.nn.dropout(clf_h, 1 - self.params["clf_pdrop"], shape)
            clf_h = tf.reshape(clf_h, [-1, self.params["n_embd"]])
            clf_logits = clf(clf_h, 1, train=train)
            clf_logits = tf.reshape(clf_logits, [-1, 2])

            clf_losses = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=clf_logits, labels=Y)
            return clf_logits, clf_losses, lm_losses

    def mgpu_train(self, *xs):
        gpu_ops = []
        gpu_grads = []
        xs = (tf.split(x, self.params["n_gpu"], 0) for x in xs)
        for i, xs in enumerate(zip(*xs)):
            do_reuse = True if i > 0 else None
            with tf.device(assign_to_gpu(i, "/gpu:0")), tf.variable_scope(tf.get_variable_scope(), reuse=do_reuse):

                clf_logits, clf_losses, lm_losses = self.model(*xs, train=True, reuse=do_reuse)
                if self.params["lm_coef"] > 0:
                    train_loss = tf.reduce_mean(clf_losses) + self.params["lm_coef"] * tf.reduce_mean(lm_losses)
                else:
                    train_loss = tf.reduce_mean(clf_losses)

                params = find_trainable_variables("model")
                grads = tf.gradients(train_loss, params)
                grads = list(zip(grads, params))
                gpu_grads.append(grads)
                gpu_ops.append([clf_logits, clf_losses, lm_losses])

        ops = [tf.concat(op, 0) for op in zip(*gpu_ops)]
        grads = average_grads(gpu_grads)
        grads = [g for g, p in grads]
        train = adam(params,
                     grads,
                     self.params["lr"],
                     partial(lr_schedules[self.params["lr_schedule"]],
                             warmup=self.params["lr_warmup"]),
                     self.n_updates_total,
                     l2=self.params["l2"],
                     max_grad_norm=self.params["max_grad_norm"],
                     vector_l2=self.params["vector_l2"],
                     b1=self.params["b1"],
                     b2=self.params["b2"],
                     e=self.params["e"])

        return [train] + ops

    def mgpu_predict(self, *xs):
        gpu_ops = []
        xs = (tf.split(x, self.params["n_gpu"], 0) for x in xs)
        for i, xs in enumerate(zip(*xs)):
            with tf.device(assign_to_gpu(i, "/gpu:0")), tf.variable_scope(tf.get_variable_scope(), reuse=True):
                clf_logits, clf_losses, lm_losses = self.model(*xs, train=False, reuse=True)
                gpu_ops.append([clf_logits, clf_losses, lm_losses])
        ops = [tf.concat(op, 0) for op in zip(*gpu_ops)]
        return ops

    def iter_apply(self, Xs, Ms, Ys):
        fns = [lambda x: np.concatenate(x, 0), lambda x: float(np.sum(x))]
        results = []
        for xmb, mmb, ymb in iter_data(Xs, Ms, Ys, n_batch=self.n_batch_train, truncate=False, verbose=True):
            n = len(xmb)
            if n == self.n_batch_train:
                res = self.sess.run([self.eval_mgpu_logits, self.eval_mgpu_clf_loss],
                                    {self.X_train: xmb, self.M_train: mmb, self.Y_train: ymb})
            else:
                res = self.sess.run([self.eval_logits, self.eval_clf_loss], {self.X: xmb, self.M: mmb, self.Y: ymb})
            res = [r * n for r in res]
            results.append(res)
        results = zip(*results)
        return [fn(res) for res, fn in zip(results, fns)]

    def iter_predict(self, Xs, Ms):
        logits = []
        for xmb, mmb in iter_data(Xs, Ms, n_batch=self.n_batch_train, truncate=False, verbose=True):
            n = len(xmb)
            if n == self.n_batch_train:
                logits.append(self.sess.run(self.eval_mgpu_logits, {self.X_train: xmb, self.M_train: mmb}))
            else:
                logits.append(self.sess.run(self.eval_logits, {self.X: xmb, self.M: mmb}))

        logits = np.concatenate(logits, 0)
        return logits

    def transform_roc(self, X1, X2, X3):

        n_batch = len(X1)
        xmb = np.zeros((n_batch,
                        2,
                        self.params["n_ctx"],
                        2),
                       dtype=np.int32)

        mmb = np.zeros((n_batch,
                        2,
                        self.params["n_ctx"]),
                       dtype=np.float32)

        start = self.encoder['_start_']
        delimiter = self.encoder['_delimiter_']
        for i, (x1, x2, x3), in enumerate(zip(X1, X2, X3)):
            x12 = [start] + x1[:self.max_len] + [delimiter] + x2[:self.max_len] + [self.clf_token]
            x13 = [start] + x1[:self.max_len] + [delimiter] + x3[:self.max_len] + [self.clf_token]
            l12 = len(x12)
            l13 = len(x13)
            xmb[i, 0, :l12, 0] = x12
            xmb[i, 1, :l13, 0] = x13
            mmb[i, 0, :l12] = 1
            mmb[i, 1, :l13] = 1
        xmb[:, :, :, 1] = np.arange(self.n_vocab + self.n_special,
                                    self.n_vocab + self.n_special + self.params["n_ctx"])
        return xmb, mmb

    def data_prep(self):

        text_encoder = TextEncoder(self.params["encoder_path"], self.params["bpe_path"])
        self.encoder = text_encoder.encoder
        self.n_vocab = len(text_encoder.encoder)

        (trX1, trX2, trX3, self.trY), (vaX1, vaX2, vaX3, self.vaY), (teX1, teX2, teX3) = \
            encode_dataset(rocstories(self.params["data_dir"]),
                           encoder=text_encoder)


        self.encoder['_start_'] = len(self.encoder)
        self.encoder['_delimiter_'] = len(self.encoder)
        self.encoder['_classify_'] = len(self.encoder)
        self.clf_token = self.encoder['_classify_']
        self.max_len = self.params["n_ctx"]//2-2

        temp = max([len(x1[:self.max_len])+max(len(x2[:self.max_len]),
                                               len(x3[:self.max_len])) for x1, x2, x3 in zip(trX1, trX2, trX3)] + \
                   [len(x1[:self.max_len])+max(len(x2[:self.max_len]),
                                               len(x3[:self.max_len])) for x1, x2, x3 in zip(vaX1, vaX2, vaX3)] + \
                   [len(x1[:self.max_len])+max(len(x2[:self.max_len]),
                                               len(x3[:self.max_len])) for x1, x2, x3 in zip(teX1, teX2, teX3)])

        self.params["n_ctx"] = min(temp + 3, self.params["n_ctx"])

        self.trX, self.trM = self.transform_roc(trX1, trX2, trX3)
        self.vaX, self.vaM = self.transform_roc(vaX1, vaX2, vaX3)
        self.teX, self.teM = self.transform_roc(teX1, teX2, teX3)

        self.n_train = len(self.trY)
        self.n_valid = len(self.vaY)
        self.n_batch_train = self.params["n_batch"] * self.params["n_gpu"]
        self.n_updates_total = (self.n_train//self.n_batch_train) * self.params["n_iter"]

        self.X_train = tf.placeholder(tf.int32, [self.n_batch_train, 2, self.params["n_ctx"], 2])
        self.M_train = tf.placeholder(tf.float32, [self.n_batch_train, 2, self.params["n_ctx"]])

        self.X = tf.placeholder(tf.int32, [None, 2, self.params["n_ctx"], 2])
        self.M = tf.placeholder(tf.float32, [None, 2, self.params["n_ctx"]])

        self.Y_train = tf.placeholder(tf.int32, [self.n_batch_train])
        self.Y = tf.placeholder(tf.int32, [None])

    def train(self):
        train, logits, clf_losses, lm_losses = self.mgpu_train(self.X_train, self.M_train, self.Y_train)
        clf_loss = tf.reduce_mean(clf_losses)

        params = find_trainable_variables('model')
        self.sess.run(tf.global_variables_initializer())

        shapes = json.load(open('model/params_shapes.json'))
        offsets = np.cumsum([np.prod(shape) for shape in shapes])
        init_params = [np.load('model/params_{}.npy'.format(n)) for n in range(10)]
        init_params = np.split(np.concatenate(init_params, 0), offsets)[:-1]
        init_params = [param.reshape(shape) for param, shape in zip(init_params, shapes)]
        init_params[0] = init_params[0][:self.params["n_ctx"]]
        init_params[0] = np.concatenate([init_params[1], (np.random.randn(self.n_special,
                                                                          self.params["n_embd"])*0.02).astype(np.float32),
                                         init_params[0]], 0)
        del init_params[1]

        if self.params["n_transfer"] == -1:
            self.params["n_transfer"] = 0
        else:
            self.params["n_transfer"] = 1 + self.params["n_transfer"] * 12

        self.sess.run([p.assign(ip) for p, ip in zip(params[:self.params["n_transfer"]],
                                                     init_params[:self.params["n_transfer"]])])

        self.eval_mgpu_logits, self.eval_mgpu_clf_losses, self.eval_mgpu_lm_losses = self.mgpu_predict(self.X_train,
                                                                                                       self.M_train,
                                                                                                       self.Y_train)

        self.eval_logits, self.eval_clf_losses, self.eval_lm_losses = self.model(self.X,
                                                                                 self.M,
                                                                                 self.Y,
                                                                                 train=False,
                                                                                 reuse=True)
        self.eval_clf_loss = tf.reduce_mean(self.eval_clf_losses)
        self.eval_mgpu_clf_loss = tf.reduce_mean(self.eval_mgpu_clf_losses)

        if self.params["dataset"] != 'stsb':
            trYt = self.trY

        self.save(os.path.join(self.params["save_dir"], self.params["desc"], 'best_params.jl'), params)

        for i in range(self.params["n_iter"]):
            for xmb, mmb, ymb in iter_data(*shuffle(self.trX, self.trM, trYt,
                                                    random_state=np.random),
                                           n_batch=self.n_batch_train,
                                           truncate=True, verbose=True):
                cost, _ = self.sess.run([clf_loss, train], {self.X_train: xmb, self.M_train: mmb, self.Y_train: ymb})
                self.n_updates += 1
                if self.n_updates in [1000, 2000, 4000, 8000, 16000, 32000] and self.n_epochs == 0:
                    self.log(params)
            self.n_epochs += 1
            self.log(params)

        self.sess.run([p.assign(ip) for p, ip in zip(params, joblib.load(os.path.join(self.params["save_dir"],
                                                                                      self.params["desc"],
                                                                                      'best_params.jl')))])

    def predict(self):
        filename = file_names[self.params["dataset"]]
        pred_fn = pred_fns[self.params["dataset"]]
        label_decoder = label_decoders[self.params["dataset"]]
        predictions = pred_fn(self.iter_predict(self.teX, self.teM))
        if label_decoder is not None:
            predictions = [label_decoder[prediction] for prediction in predictions]
        path = os.path.join(self.params["submission_dir"], filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('{}\t{}\n'.format('index', 'prediction'))
            for i, prediction in enumerate(predictions):
                f.write('{}\t{}\n'.format(i, prediction))

        rocstories_analysis(self.params["data_dir"],
                            os.path.join(self.params["submission_dir"], 'ROCStories.tsv'),
                            os.path.join(self.params["log_dir"], 'rocstories.jsonl'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--desc', type=str)
    parser.add_argument('--dataset', type=str)
    parser.add_argument('--log_dir', type=str, default='log/')
    parser.add_argument('--save_dir', type=str, default='save/')
    parser.add_argument('--data_dir', type=str, default='data/')
    parser.add_argument('--submission_dir', type=str, default='submission/')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--n_iter', type=int, default=3)
    parser.add_argument('--n_batch', type=int, default=8)
    parser.add_argument('--max_grad_norm', type=int, default=1)
    parser.add_argument('--lr', type=float, default=6.25e-5)
    parser.add_argument('--lr_warmup', type=float, default=0.002)
    parser.add_argument('--n_ctx', type=int, default=512)
    parser.add_argument('--n_embd', type=int, default=768)
    parser.add_argument('--n_head', type=int, default=12)
    parser.add_argument('--n_layer', type=int, default=12)
    parser.add_argument('--embd_pdrop', type=float, default=0.1)
    parser.add_argument('--attn_pdrop', type=float, default=0.1)
    parser.add_argument('--resid_pdrop', type=float, default=0.1)
    parser.add_argument('--clf_pdrop', type=float, default=0.1)
    parser.add_argument('--l2', type=float, default=0.01)
    parser.add_argument('--vector_l2', action='store_true')
    parser.add_argument('--n_gpu', type=int, default=4)
    parser.add_argument('--opt', type=str, default='adam')
    parser.add_argument('--afn', type=str, default='gelu')
    parser.add_argument('--lr_schedule', type=str, default='warmup_linear')
    parser.add_argument('--encoder_path', type=str, default='model/encoder_bpe_40000.json')
    parser.add_argument('--bpe_path', type=str, default='model/vocab_40000.bpe')
    parser.add_argument('--n_transfer', type=int, default=12)
    parser.add_argument('--lm_coef', type=float, default=0.5)
    parser.add_argument('--b1', type=float, default=0.9)
    parser.add_argument('--b2', type=float, default=0.999)
    parser.add_argument('--e', type=float, default=1e-8)

    args = parser.parse_args()

    m = Model(args.__dict__)

    m.data_prep()
    m.train()
    m.predict()

