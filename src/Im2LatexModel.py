#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
    Copyright 2017 Sumeet S Singh

    This file is part of im2latex solution by Sumeet S Singh.

    This program is free software: you can redistribute it and/or modify
    it under the terms of the Affero GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    Affero GNU General Public License for more details.

    You should have received a copy of the Affero GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

Created on Sat Jul  8 19:33:38 2017
Tested on python 2.7

@author: Sumeet S Singh
"""

import collections
import itertools
import dl_commons as dlc
import tf_commons as tfc
import tensorflow as tf
from keras.applications.vgg16 import VGG16
from keras import backend as K
from CALSTM import CALSTM, CALSTMState
from hyper_params import Im2LatexModelParams
from data_reader import InpTup
BeamSearchDecoder = tf.contrib.seq2seq.BeamSearchDecoder

def build_image_context(params, image_batch):
    ## Conv-net
    assert K.int_shape(image_batch) == (params.B,) + params.image_shape
    ################ Build VGG Net ################
    with tf.variable_scope('VGGNet'):
        K.set_image_data_format('channels_last')
        convnet = VGG16(include_top=False, weights='imagenet', pooling=None,
                        input_shape=params.image_shape,
                        input_tensor = image_batch)
        convnet.trainable = False
        for layer in convnet.layers:
            layer.trainable = False

        print 'convnet output_shape = ', convnet.output_shape
        ## a = convnet(image_batch)
        a = convnet.output
        assert K.int_shape(a)[1:] == (params.H, params.W, params.D)

        ## Combine HxW into a single dimension L
        a = tf.reshape(a, shape=(params.B or -1, params.L, params.D))
        assert K.int_shape(a) == (params.B, params.L, params.D)

    return a

Im2LatexState = collections.namedtuple('Im2LatexState', ('calstm_states', 'yProbs'))
class Im2LatexModel(tf.nn.rnn_cell.RNNCell):
    """
    One timestep of the decoder model. The entire function can be seen as a complex RNN-cell
    that includes a LSTM stack and an attention model.
    """
    def __init__(self, params, beamsearch_width=1, reuse=True):
        """
        Args:
            params (Im2LatexModelParams)
            beamsearch_width (integer): Only used when inferencing with beamsearch. Otherwise set it to 1.
                Will cause the batch_size in internal assert statements to get multiplied by beamwidth.
            reuse: Sets the variable_reuse setting for the entire object.
        """
        self._params = self.C = Im2LatexModelParams(params)
        with tf.variable_scope('Im2LatexStack', reuse=reuse) as outer_scope:
            self.outer_scope = outer_scope
            with tf.variable_scope('Inputs') as scope:
                self._inp_q = tf.FIFOQueue(self.C.input_queue_capacity,
                                           (self.C.int_type, self.C.int_type,
                                            self.C.int_type, self.C.int_type,
                                            self.C.dtype))
                inp_tup = InpTup(*self._inp_q.dequeue())
                self._y_s = inp_tup.y_s
                self._seq_len = inp_tup.seq_len
                self._y_ctc = inp_tup.y_ctc
                self._ctc_len = inp_tup.ctc_len

                if self._params.build_image_context:
                    ## Image features/context from the Conv-Net
                    ## self._im = tf.placeholder(dtype=self.C.dtype, shape=((self.C.B,)+self.C.image_shape), name='image')
                    self._im = inp_tup.im
                    self._a = build_image_context(params, self._im)
                else:
                    ## self._a = tf.placeholder(dtype=self.C.dtype, shape=(self.C.B, self.C.L, self.C.D), name='a')
                    self._a = inp_tup.im

            ## Set tensor shapes because they get forgotten by the queue
            self._a.set_shape((self.C.B, self.C.L, self.C.D))
            self._y_s.set_shape((self.C.B, None))
            self._seq_len.set_shape((self.C.B,))
            self._y_ctc.set_shape((self.C.B, None))
            self._ctc_len.set_shape((self.C.B,))

            ## RNN portion of the model
            with tf.variable_scope('Im2LatexRNN') as scope:
                super(Im2LatexModel, self).__init__(_scope=scope, name=scope.name)
                self._rnn_scope = scope

                ## Beam Width to be supplied to BeamsearchDecoder. It essentially broadcasts/tiles a
                ## batch of input from size B to B * BeamWidth. Set this value to 1 in the training
                ## phase.
                self._beamsearch_width = beamsearch_width

                ## First step of x_s is 1 - the begin-sequence token. Shape = (T, B); T==1
                self._x_0 = tf.ones(shape=(1, self.C.B*beamsearch_width),
                                    dtype=self.C.int_type,
                                    name='begin_sequence')

                self._calstms = []
                for i, rnn_params in enumerate(self.C.D_RNN, start=1):
                    with tf.variable_scope('CALSTM_%d'%i) as var_scope:
                        self._calstms.append(CALSTM(rnn_params, self._a, beamsearch_width, var_scope))
                self._CALSTM_stack = tf.nn.rnn_cell.MultiRNNCell(self._calstms)
                self._num_calstm_layers = len(self.C.D_RNN)

                with tf.variable_scope('Embedding') as embedding_scope:
                    self._embedding_scope = embedding_scope
                    self._embedding_matrix = tf.get_variable('Embedding_Matrix',
                                                             (self.C.K, self.C.m),
                                                             initializer=self.C.embeddings_initializer,
                                                             trainable=True)
            ## Init State Model
            self._init_state_model = self._init_state()

    @property
    def BeamWidth(self):
        return self._beamsearch_width

    @property
    def RuntimeBatchSize(self):
        return self.C.B * self.BeamWidth

#    def _set_beamwidth(self, beamwidth):
#        self._beamsearch_width = beamwidth
#        for calstm in self._calstms:
#            calstm._set_beamwidth(beamwidth)

    def _output_layer(self, Ex_t, h_t, z_t):
        with tf.variable_scope(self._rnn_scope) as var_scope:
            with tf.name_scope(var_scope.original_name_scope):
                with tf.variable_scope('Output_Layer'):
                    ## Renaming HyperParams for convenience
                    CONF = self.C
                    B = self.RuntimeBatchSize
                    D = self.C.D
                    m = self.C.m
                    Kv =self.C.K
                    n = self._CALSTM_stack.output_size

                    assert K.int_shape(Ex_t) == (B, m)
                    assert K.int_shape(h_t) == (B, self._CALSTM_stack.output_size)
                    assert K.int_shape(z_t) == (B, D)

                    ## First layer of output MLP
                    if CONF.output_follow_paper: ## Follow the paper.
                        ## Affine transformation of h_t and z_t from size n/D to bring it down to m
                        o_t = tfc.FCLayer({'num_units':m, 'activation_fn':None, 'tb':CONF.tb},
                                          batch_input_shape=(B,n+D))(tf.concat([h_t, z_t], -1)) # o_t: (B, m)
                        ## h_t and z_t are both dimension m now. So they can now be added to Ex_t.
                        o_t = o_t + Ex_t # Paper does not multiply this with weights - weird.
                        ## non-linearity for the first layer
                        o_t = tfc.Activation(CONF, batch_input_shape=(B,m))(o_t)
                        dim = m
                    else: ## Use a straight MLP Stack
                        o_t = K.concatenate((Ex_t, h_t, z_t)) # (B, m+n+D)
                        dim = m+n+D

                    ## Regular MLP layers
                    assert CONF.output_layers.layers_units[-1] == Kv
                    logits_t = tfc.MLPStack(CONF.output_layers, batch_input_shape=(B,dim))(o_t)

                    assert K.int_shape(logits_t) == (B, Kv)
                    return tf.nn.softmax(logits_t), logits_t

    @property
    def output_size(self):
        return self.C.K

    @property
    def state_size(self):
        return Im2LatexState(self._CALSTM_stack.state_size, self.C.K)

    def zero_state(self, batch_size, dtype):
        return Im2LatexState(self._CALSTM_stack.zero_state(batch_size, dtype),
                             tf.zeros((batch_size, self.C.K), dtype=dtype, name='yProbs'))

    def _recur_init_FCLayers(self, zero_state, counter, params, inp):
        """
        Creates FC layers for lstm_states (init_c and init_h) which will sit atop the init-state MLP.
        It does this by replacing each instance of 'h' or 'c' with a FC layer using the given params.
        This recursive function is only intended to be invoked by _init_state() and therefore is
        scoped under OutputLayers.
        """
        with tf.variable_scope(self._init_FC_scope) as var_scope:
            with tf.name_scope(var_scope.original_name_scope):
                if counter is None:
                    counter = itertools.count(1)

                assert dlc.issequence(zero_state)

                s = zero_state
                if hasattr(s, 'h'):
                    num_units = K.int_shape(s.h)[1]
                    layer_params = tfc.FCLayerParams(params).updated({'num_units':num_units})
                    s = s._replace(h = tfc.FCLayer(layer_params)(inp, counter.next()))
                if hasattr(s, 'c'):
                    num_units = K.int_shape(s.c)[1]
                    layer_params = tfc.FCLayerParams(params).updated({'num_units':num_units})
                    s = s._replace(c=tfc.FCLayer(layer_params)(inp, counter.next()))

                ## Create a mutable list from the immutable tuple
                lst = []
                for i in xrange(len(s)):
                    if dlc.issequence(s[i]):
                        lst.append(self._recur_init_FCLayers(s[i], counter, params, inp))
                    else:
                        lst.append(s[i])

                ## Create the tuple back from the list
                if hasattr(s, '_make'):
                    s = s._make(lst)
                else:
                    s = tuple(lst)

                ## Set htop to the topmost 'h' of the LSTM stack
        #        if hasattr(s, 'htop') and isinstance(s, CALSTMState):
        #            ## s.lstm_state can be a single LSTMStateTuple or a tuple of LSTMStateTuples
        #            s.htop = s.lstm_state.h if hasattr(s.lstm_state, 'h') else s.lstm_state[-1].h

                return s

    def _init_state(self):
        ################ Initializer MLP ################
        with tf.variable_scope(self.outer_scope):
            ## ugly, but only option to get pretty tensorboard visuals
            with tf.name_scope(self.outer_scope.original_name_scope):
                with tf.variable_scope('Initializer_MLP'):

                    ## Broadcast im_context to BeamWidth
                    if self.BeamWidth > 1:
                        a = K.tile(self._a, (self.BeamWidth,1,1))
                        batchsize = self.RuntimeBatchSize
                    else:
                        a = self._a
                        batchsize = self.C.B
                    ## As per the paper, this is a multi-headed MLP. It has a base stack of common layers, plus
                    ## one additional output layer for each of the h and c LSTM states. So if you had
                    ## configured - say 3 CALSTM-stacks with 2 LSTM cells per CALSTM-stack you would end-up with
                    ## 6 top-layers on top of the base MLP stack. Base MLP stack is specified in param 'init_model'
                    a = K.mean(a, axis=1) # final shape = (B, D)
                    a = tfc.MLPStack(self.C.init_model)(a)

                    counter = itertools.count(0)
                    def zero_to_init_state(zs, counter):
                        assert isinstance(zs, Im2LatexState)
                        cs = zs.calstm_states
                        assert isinstance(cs, tuple) and not isinstance(cs, CALSTMState)
                        lst = []
                        for i in xrange(len(cs)):
                            assert isinstance(cs[i], CALSTMState)
                            lst.append(self._recur_init_FCLayers(cs[i],
                                                                 counter,
                                                                 self.C.init_model_final_layers,
                                                                 a))

                        cs = tuple(lst)

                        return zs._replace(calstm_states=cs)

                    with tf.variable_scope('Output_Layers'):
                        self._init_FC_scope = tf.get_variable_scope()
                        init_state = self.zero_state(batchsize, dtype=self.C.dtype)
                        init_state = zero_to_init_state(init_state, counter)

        return init_state

    def _embedding_lookup(self, ids):
        with tf.variable_scope(self._embedding_scope) as scope:
            with tf.name_scope(scope.original_name_scope):
                m = self.C.m
                assert self._embedding_matrix is not None
                #assert K.int_shape(ids) == (B,)
                shape = list(K.int_shape(ids))
                embedded = tf.nn.embedding_lookup(self._embedding_matrix, ids)
                shape.append(m)
                ## Embedding lookup forgets the leading dimensions (e.g. (B,))
                ## Fix that here.
                embedded.set_shape(shape) # (...,m)
                return embedded

    def call(self, Ex_t, state):
        """
        One step of the RNN API of this class.
        Layers a deep-output layer on top of CALSTM
        """
        with tf.variable_scope(self._rnn_scope) as var_scope:
            with tf.name_scope(var_scope.original_name_scope):## ugly, but only option to get pretty tensorboard visuals
                ## State
                calstm_states_t_1 = state.calstm_states
                ## CALSTM stack
                htop_t, calstm_states_t = self._CALSTM_stack(Ex_t, calstm_states_t_1)
                ## Output layer
                yProbs_t, yLogits_t = self._output_layer(Ex_t, htop_t, calstm_states_t[-1].ztop)

                return yLogits_t, Im2LatexState(calstm_states_t, yProbs_t)

    ScanOut = collections.namedtuple('ScanOut', ('yLogits', 'state'))
    def _scan_step_training(self, out_t_1, x_t):
        with tf.variable_scope('Ey'):
            Ex_t = self._embedding_lookup(x_t)

        ## RNN.__call__
        yLogits_t, state_t = self(Ex_t, out_t_1[1], scope=self._rnn_scope)
        return self.ScanOut(yLogits_t, state_t)

    def build_train_graph(self):
        with tf.variable_scope(self.outer_scope):
            with tf.name_scope(self.outer_scope.original_name_scope):## ugly, but only option to get pretty tensorboard visuals
#                with tf.variable_scope('TrainingGraph'):
                ## tf.scan requires time-dimension to be the first dimension
                y_s = K.permute_dimensions(self._y_s, (1, 0)) # (T, B)

                ################ Build x_s ################
                ## x_s is y_s time-delayed by 1 timestep. First token is 1 - the begin-sequence token.
                ## last token of y_s which is <eos> token (zero) will not appear in x_s
                x_s = K.concatenate((self._x_0, y_s[0:-1]), axis=0)

                """ Build the training graph of the model """
                accum = self.ScanOut(tf.zeros(shape=(self.RuntimeBatchSize, self.C.K), dtype=self.C.dtype),
                                     self._init_state_model)
                out_s = tf.scan(self._scan_step_training, x_s,
                                initializer=accum, swap_memory=True)
                ## yLogits_s, yProbs_s, alpha_s = out_s.yLogits, out_s.state.yProbs, out_s.state.calstm_state.alpha
                ## SCRATCHED: THIS IS ONLY ACCURATE FOR 1 CALSTM LAYER. GATHER ALPHAS OF LOWER CALSTM LAYERS.
                yLogits_s = out_s.yLogits
                alpha_s_n = tf.stack([cs.alpha for cs in out_s.state.calstm_states], axis=0) # (N, T, B, L)
                ## Switch the batch dimension back to first position - (B, T, ...)
                ## yProbs = K.permute_dimensions(yProbs_s, [1,0,2])
                yLogits = K.permute_dimensions(yLogits_s, [1,0,2])
                alpha = K.permute_dimensions(alpha_s_n, [0,2,1,3]) # (N, B, T, L)

                train_ops = self._optimizer(yLogits,
                                            self._y_s,
                                            alpha,
                                            self._seq_len,
                                            self._y_ctc,
                                            self._ctc_len).updated({
                                                'inp_q':self._inp_q,
                                                'tb_logs': tf.summary.merge_all()}
                                                )

                return train_ops


    def _optimizer(self, yLogits, y_s, alpha, sequence_lengths, y_ctc, ctc_len):
        with tf.variable_scope(self.outer_scope) as var_scope:
            with tf.name_scope(var_scope.original_name_scope):
                B = self.C.B
                Kv =self.C.K
                L = self.C.L
                N = self._num_calstm_layers

                assert K.int_shape(yLogits) == (B, None, Kv) # (B, T, K)
                assert K.int_shape(alpha) == (N, B, None, L) # (N, B, T, L)
                assert K.int_shape(y_s) == (B, None) # (B, T)
                assert K.int_shape(sequence_lengths) == (B,)
                assert K.int_shape(y_ctc) == (B, None) # (B, T)
                assert K.int_shape(ctc_len) == (B,)

                ################ Build Cost Function ################
                with tf.variable_scope('Cost'):
                    sequence_mask = tf.sequence_mask(sequence_lengths, maxlen=tf.shape(y_s)[1],
                                                     dtype=self.C.dtype) # (B, T)
                    assert K.int_shape(sequence_mask) == (B,None) # (B,T)

                    ## Masked negative log-likelihood of the sequence.
                    ## Note that log(product(p_t)) = sum(log(p_t)) therefore taking log of
                    ## joint-sequence-probability is same as taking sum of log of probability at each time-step

                    ## Compute Sequence Log-Loss / Log-Likelihood = -Log( product(p_t) ) = -sum(Log(p_t))
                    if self.C.sum_logloss:
                        ## Here we do not normalize the log-loss across time-steps because the
                        ## paper as well as it's source-code do not do that.
                        log_losses = tf.contrib.seq2seq.sequence_loss(logits=yLogits,
                                                                       targets=y_s,
                                                                       weights=sequence_mask,
                                                                       average_across_timesteps=False,
                                                                       average_across_batch=False)
                        # print 'shape of loss_vector = %s'%(K.int_shape(log_losses),)
                        log_losses = tf.reduce_sum(log_losses, axis=1) # sum along time dimension => (B,)
                        # print 'shape of loss_vector = %s'%(K.int_shape(log_losses),)
                        log_likelihood = tf.reduce_mean(log_losses, axis=0, name='CrossEntropyPerSentence') # scalar
                    else: ## Standard log perplexity (average per-word log perplexity)
                        log_losses = tf.contrib.seq2seq.sequence_loss(logits=yLogits,
                                                                       targets=y_s,
                                                                       weights=sequence_mask,
                                                                       average_across_timesteps=True,
                                                                       average_across_batch=False)
                        # print 'shape of loss_vector = %s'%(K.int_shape(log_losses),)
                        log_likelihood = tf.reduce_mean(log_losses, axis=0, name='CrossEntropyPerWord')
                    assert K.int_shape(log_likelihood) == tuple()
                    alpha_mask =  tf.expand_dims(sequence_mask, axis=2) # (B, T, 1)
                    ## Calculate the alpha penalty: lambda * sum_over_i&b(square(C/L - sum_over_t(alpha_i)))
                    ##
                    if self.C.MeanSumAlphaEquals1:
                        mean_sum_alpha_i = 1.0
                    else:
                        mean_sum_alpha_i = tf.div(tf.cast(sequence_lengths, dtype=self.C.dtype), tf.cast(self.C.L, dtype=self.C.dtype)) # (B,)
                        mean_sum_alpha_i = tf.expand_dims(mean_sum_alpha_i, axis=1) # (B, 1)

        #                sum_over_t = tf.reduce_sum(tf.multiply(alpha,sequence_mask), axis=1, keep_dims=False)# (B, L)
        #                squared_diff = tf.squared_difference(sum_over_t, mean_sum_alpha_i) # (B, L)
        #                alpha_penalty = self.C.pLambda * tf.reduce_sum(squared_diff, keep_dims=False) # scalar
                    sum_over_t = tf.reduce_sum(tf.multiply(alpha, alpha_mask), axis=2, keep_dims=False)# (N, B, L)
                    squared_diff = tf.squared_difference(sum_over_t, mean_sum_alpha_i) # (N, B, L)
                    alpha_penalty = self.C.pLambda * tf.reduce_sum(squared_diff, keep_dims=False) # scalar
                    mean_sum_alpha_i = tf.reduce_mean(mean_sum_alpha_i)
                    mean_seq_len = tf.reduce_mean(tf.cast(sequence_lengths, dtype=tf.float32))
                    mean_sum_alpha_i2 = tf.reduce_mean(sum_over_t)
                    assert K.int_shape(alpha_penalty) == tuple()
                ################ Build CTC Cost Function ################
                ## Compute CTC loss/score with intermediate blanks removed. We've removed all spaces/blanks in the
                ## target sequences (y_ctc). Hence the target (y_ctc_ sequences are shorter than the inputs (y_s/x_s).
                ## Using CTC loss will have the following side-effect:
                ##  1) The network will be told that it is okay to omit blanks (spaces) or emit multiple blanks
                ##     since CTC will ignore those. This makes the learning easier, but we'll need to insert blanks
                ##     between tokens at inferencing step.
                with tf.variable_scope('CTC_Cost'):
                    ## sparse tensor
        #            y_idx =    tf.where(tf.not_equal(y_ctc, 0)) ## null-terminator/EOS is removed :((
                    ctc_mask = tf.sequence_mask(ctc_len, maxlen=tf.shape(y_ctc)[1], dtype=tf.bool)
                    assert K.int_shape(ctc_mask) == (B,None) # (B,T)
                    y_idx =    tf.where(ctc_mask)
                    y_vals =   tf.gather_nd(y_ctc, y_idx)
                    y_sparse = tf.SparseTensor(y_idx, y_vals, tf.shape(y_ctc, out_type=tf.int64))
                    ctc_losses = tf.nn.ctc_loss(y_sparse,
                                              yLogits,
                                              sequence_lengths,
                                              ctc_merge_repeated=False,
                                              time_major=False)
                    print 'shape of ctc_losses = %s'%(K.int_shape(ctc_losses),)
                    assert K.int_shape(ctc_losses) == (B, )
                    if self.C.sum_logloss:
                        ctc_loss = tf.reduce_mean(ctc_losses, axis=0, name='CTCSentenceLoss') # scalar
                    else: # mean loss per word
                        ctc_loss = tf.div(tf.reduce_sum(ctc_losses, axis=0), tf.reduce_sum(tf.cast(ctc_mask, dtype=self.C.dtype)), name='CTCWordLoss') # scalar
                    assert K.int_shape(ctc_loss) == tuple()
                if self.C.use_ctc_loss:
                    cost = ctc_loss + alpha_penalty
                else:
                    cost = log_likelihood + alpha_penalty

                tf.summary.scalar('training/logloss/', log_likelihood)
                tf.summary.scalar('training/ctc_loss/', ctc_loss)
                tf.summary.scalar('training/alpha_penalty/', alpha_penalty)
                tf.summary.scalar('training/total_cost/', cost)

                # Optimizer
                with tf.variable_scope('Optimizer'):
                    global_step = tf.get_variable('global_step', dtype=self.C.int_type, trainable=False, initializer=0)
                    optimizer = tf.train.AdamOptimizer(learning_rate=self.C.adam_alpha)
                    train = optimizer.minimize(cost, global_step=global_step)
                    ##tf_optimizer = tf.train.GradientDescentOptimizer(tf_rate).minimize(tf_loss, global_step=tf_step,
                    ##                                                               name="optimizer")

                return dlc.Properties({
                        'train': train,
                        'log_likelihood': log_likelihood,
                        'ctc_loss': ctc_loss,
                        'alpha_penalty': alpha_penalty,
                        'cost': cost,
                        'global_step':global_step,
                        'mean_sum_alpha_i': mean_sum_alpha_i,
                        'mean_sum_alpha_i2': mean_sum_alpha_i2,
                        'mean_seq_len': mean_seq_len
                        })

    def _beamsearch(self):
        """ Build the prediction graph of the model using beamsearch """
        with tf.variable_scope(self.outer_scope) as var_scope:
            assert var_scope.reuse == True
            ## ugly, but only option to get proper tensorboard visuals
            with tf.name_scope(self.outer_scope.original_name_scope):
                with tf.variable_scope('BeamSearch'):
                    begin_tokens = tf.ones(shape=(self.C.B,), dtype=self.C.int_type)
                    class BeamSearchDecoder2(BeamSearchDecoder):
                        def initialize(self, name=None):
                            finished, start_inputs, initial_state = BeamSearchDecoder.initialize(self, name)
                            return tf.expand_dims(finished, axis=2), start_inputs, initial_state

                    decoder =                    BeamSearchDecoder(self,
                                                                self._embedding_lookup,
                                                                begin_tokens,
                                                                0,
                                                                self._init_state_model,
                                                                beam_width=self.BeamWidth)
                    final_outputs, final_state, final_sequence_lengths = tf.contrib.seq2seq.dynamic_decode(
                                                                    decoder,
                                                                    impute_finished=False,
                                                                    maximum_iterations=self.C.Max_Seq_Len+10,
                                                                    swap_memory=True)
                    assert K.int_shape(final_outputs.predicted_ids) == (self.C.B, None, self.BeamWidth)
                    assert K.int_shape(final_outputs.beam_search_decoder_output.scores) == (self.C.B, None, self.BeamWidth)
                    assert K.int_shape(final_sequence_lengths) == (self.C.B, self.BeamWidth)
                    print('final_outputs:%s\n, final_seq_lens:%s'%(final_outputs, final_sequence_lengths))
                    #ids = tf.not_equal(final_outputs.predicted_ids, 0)
                    return dlc.Properties({
                            'ids': final_outputs.predicted_ids,
                            'scores': final_outputs.beam_search_decoder_output.scores,
                            'seq_lens': final_sequence_lengths
                            })

    def test(self):
        """ Test one batch of input """
        B = self.C.B
        BW = self.BeamWidth

        with tf.variable_scope(self.outer_scope) as var_scope:
            assert var_scope.reuse == True
            ## ugly, but only option to get proper tensorboard visuals
            with tf.name_scope(self.outer_scope.original_name_scope):
                outputs = self._beamsearch()
                with tf.name_scope('BeamSearch_Results'):
                    scores = tf.transpose(outputs.scores, perm=[0,2,1]) # (B, BeamWidth, T)
                    scores = tf.reshape(scores, shape=(B*BW, -1)) # (B*BeamWidth, T)
                    ids = tf.transpose(outputs.ids, perm=(0,2,1)) # (B, BeamWidth, T)
                    ids = tf.reshape(ids, shape=(B*BW, -1)) # B*BeamWidth, T)
                    seq_lens = tf.reshape(outputs.seq_lens, shape=[-1]) # (B*BeamWidth,)
                    mask = tf.sequence_mask(seq_lens, maxlen=tf.shape(scores)[1], dtype=tf.int32) # (B*BeamWidth, T)
                    # scores = log-probabilities are negative values
                    # zero out scores (log probabilities) of words after EOS. Tantamounts to setting their probs = 1
                    # Hence they will have no effect on the sequence probabilities
                    scores = scores * tf.to_float(mask) # (B*BeamWidth, T)
                    ## Also zero out tokens after EOS because we set impute_finished = False and as a result we noticed
                    ## ID values = -1 after EOS tokens.
                    ## Conveniently, zero is also the EOS token.
                    ids = ids*mask
                    ## Sum of log-probabilities == Product of probabilities
                    seq_scores = tf.reduce_sum(scores, axis=1) # (B*BeamWidth,)
                    seq_scores = tf.reshape(seq_scores, shape=(B, BW))

                ## Select the top scoring beams
                with tf.name_scope('SelectTopK'):
                    ids = tf.reshape(ids, shape=(B, BW, -1)) # (B, BeamWidth, T)
                    seq_lens = tf.reshape(seq_lens, shape=(B, BW)) #(B, BeamWidth)
                    k = min([5, BW])
                    topK_seq_scores, topK_score_indices = tfc.batch_top_k_2D(seq_scores, k) # (B, k) and (B, k, 2) sorted
                    topK_ids = tfc.batch_slice(ids, topK_score_indices) # (B, k, T)
                    assert K.int_shape(topK_ids) == (B, k, None)
                    topK_seq_lens = tfc.batch_slice(seq_lens, topK_score_indices) # (B, k)
                    assert K.int_shape(topK_seq_lens) == (B, k)

                with tf.name_scope('Instrumentation'):
                    tf.summary.histogram( 'prediction/score/predicted/score/', topK_seq_scores[:,0], collections=['validation'])
                    tf.summary.histogram( 'prediction/score/predicted/seq_len/', topK_seq_lens[:,0], collections=['validation'])
                    tf.summary.histogram( 'prediction/score/top_%d/score/'%k, topK_seq_scores, collections=['validation'])
                    tf.summary.histogram( 'prediction/score/top_%d/seq_len/'%k, topK_seq_lens, collections=['validation'])

                ## CTC accuracy metric - i.e. comparison of squashed output and target sequences
                with tf.name_scope('ctc_accuracy'):
                    assert not self.C.dtype.is_unsigned
                    ## Place all logit mass on predicted IDs
                    pseudo_probs = []
                    topK_ids_logits = tf.one_hot(topK_ids, depth=self.C.K, on_value=self.C.dtype.max, off_value=self.C.dtype.min)#(B,k,T,K)
                    for j in range(k):
                        losses = tfc.ctc_loss(topK_ids_logits[:,j], topK_seq_lens[:,j], self._y_ctc, self._ctc_len, B, self.C.K) #(B,)
                        ## pseudo_prob should be == 1 for matching sequences and 0 for non matching
                        pseudo_prob = tf.exp(-1*losses)  # (B,)
                        pseudo_probs.append(pseudo_prob)
                    topK_match = tf.stack(pseudo_probs, axis=1) # (B, k)
                    top1_score_ctc_accuracy = tf.reduce_mean(topK_match[:,0]*100.)
                    best_of_topK, best_of_topK_indices = tfc.batch_top_k_2D(topK_match, k=1) # (B,1), (B*1, 2)
                    best_of_topK_ctc_accuracy = tf.reduce_mean(best_of_topK*100.)
                    best_of_topK_lens = tfc.batch_slice(topK_seq_lens, best_of_topK_indices) # (B, 1)

                    with tf.name_scope('Instrumentation'):
                        tf.summary.scalar( 'prediction/accuracy/predicted/ctc_accuracy/', best_of_topK_ctc_accuracy, collections=['validation'])
                        tf.summary.histogram( 'prediction/accuracy/predicted/seq_len/', best_of_topK_lens[:,0], collections=['validation'])
                        tf.summary.histogram( 'prediction/accuracy/top_%d/seq_len/'%k, (best_of_topK_lens), collections=['validation'])
                        tf.summary.scalar( 'prediction/score/top_%d/ctc_accuracy/'%k, top1_score_ctc_accuracy, collections=['validation'])
                logs_v = tf.summary.merge_all(key='validation')

                ## BLEU scores
                # with tf.name_scope('BLEU'):
                #     ## BLEU score is calculated outside of TensorFlow and then injected back in via. a placeholder
                #     ph_bleu = tf.placeholder(tf.float32, shape=(self.C.B,), name="BLEU_placeholder")
                #     tf.summary.histogram( 'prediction/accuracy/predicted/bleu/', ph_bleu, collections=['bleu'])
                #     tf.summary.scalar( 'prediction/accuracy/predicted/bleuH/', tf.reduce_mean(ph_bleu), collections=['bleu'])
                #     logs_b = tf.summary.merge_all(key='bleu')

                ## Edit/Levenshtein distance scores
                with tf.name_scope('LevenshteinDistance'):
                    y_ctc_beams = tf.tile(tf.expand_dims(self._y_ctc, axis=1), multiples=[1,k,1])
                    ctc_len_beams = tf.tile(tf.expand_dims(self._ctc_len, axis=1), multiples=[1,k])
                    ed = tfc.k_edit_distance(B, k, topK_ids, topK_seq_lens, y_ctc_beams, ctc_len_beams, self._params.SpaceTokenID)
                    top1_score_ed = tf.reduce_mean(ed[:,0])
                    ## Best of top_k
                    best_of_topK_ed = tf.reduce_mean(tf.reduce_min(ed, axis=1))
                    with tf.name_scope('Instrumentation'):
                        tf.summary.scalar( 'prediction/accuracy/predicted/edit_distance/', top1_score_ed, collections=['edit_distance'])
                        tf.summary.scalar( 'prediction/accuracy/top_%d/edit_distance/'%k, best_of_topK_ed, collections=['edit_distance'])
                        logs_ed = tf.summary.merge_all(key='edit_distance')

                with tf.name_scope('AggregateMetrics'):
                    ph_seq_lens = tf.placeholder(self.C.dtype)
                    ph_edit_distance = tf.placeholder(self.C.dtype)
                    ph_BoK_distance =  tf.placeholder(self.C.dtype)
                    ph_ctc_accuracy =  tf.placeholder(self.C.dtype)
                    ph_BoK_ctc_accuracy =  tf.placeholder(self.C.dtype)
                    ph_valid_time =  tf.placeholder(self.C.dtype)
                    tf.summary.histogram( 'prediction/aggregate/seq_lens/', ph_seq_lens, collections=['aggregate'])
                    tf.summary.scalar( 'prediction/aggregate/predicted/edit_distance/', ph_edit_distance, collections=['aggregate'])
                    tf.summary.scalar( 'prediction/aggregate/bestof_%d/edit_distance/'%k, ph_BoK_distance, collections=['aggregate'])
                    tf.summary.scalar( 'prediction/aggregate/predicted/ctc_accuracy/', ph_ctc_accuracy, collections=['aggregate'])
                    tf.summary.scalar( 'prediction/aggregate/bestof_%d/ctc_accuracy/'%k, ph_BoK_ctc_accuracy, collections=['aggregate'])
                    tf.summary.scalar( 'prediction/aggregate/time_per100/', ph_valid_time, collections=['aggregate'])
                    logs_aggregate = tf.summary.merge_all(key='aggregate')

                return dlc.Properties({
                    'topK_ids': topK_ids, # (B, k, T)
                    'topK_scores': topK_seq_scores, # (B, k)
                    'topK_lens': topK_seq_lens, # (B,k)
                    'topK_beams': topK_score_indices[:,:,1], # (B, k)
                    'all_ids': ids, # (B, BeamWidth, T),
                    'all_id_scores': tf.reshape(scores, shape=(B, BW, -1)), # (B, BeamWidth, T)
                    'all_seq_lens': outputs.seq_lens, # (B, BeamWidth)
                    'best_of_topK_ctc_accuracy': best_of_topK_ctc_accuracy, # scalar
                    'top1_score_ctc_accuracy': top1_score_ctc_accuracy, # scalar
                    'accuracy_probs': pseudo_probs,
                    'inp_q': self._inp_q,
                    'top1_score_ed': top1_score_ed,
                    'best_of_topK_ed': best_of_topK_ed,
                    'logs_v': logs_v,
                    'logs_ed': logs_ed,
                    'logs_aggregate': logs_aggregate,
                    'ph_seq_lens': ph_seq_lens,
                    'ph_edit_distance': ph_edit_distance,
                    'ph_BoK_distance': ph_BoK_distance,
                    'ph_ctc_accuracy': ph_ctc_accuracy,
                    'ph_BoK_ctc_accuracy': ph_BoK_ctc_accuracy,
                    'ph_valid_time': ph_valid_time
                    })