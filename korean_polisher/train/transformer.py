"""
Transformer 모델
"""
import os
import joblib
import numpy as np
import tensorflow as tf

from .scheduler import CustomSchedule
from .options import *
from ..dataset import tokenize_batch


tf.keras.backend.set_floatx(float_dtype_str)  # 모델에서 사용할 자료형 설정

train_step_signature = [
    tf.TensorSpec(shape=(None, None), dtype=int_dtype),
    tf.TensorSpec(shape=(None, None), dtype=int_dtype),
]


def gelu(x):
    # gelu activation
    return 0.5 * x * (1 + tf.tanh(tf.sqrt(2 / tf.cast(np.pi, float_dtype)) * (x + 0.044715 * tf.pow(x, 3))))


def get_angles(pos, i, d_model):
    # positional_encoding에서 사용
    angle_rates = 1 / np.power(10000, (2 * (i // 2)) / float_dtype_np(d_model))
    return pos * angle_rates


def positional_encoding(position, d_model):
    angle_rads = get_angles(np.arange(position)[:, np.newaxis],
                            np.arange(d_model)[np.newaxis, :],
                            d_model)

    # 짝수 부분에 sin 적용
    angle_rads[:, 0::2] = np.sin(angle_rads[:, 0::2])

    # 홀수 부분에 cos 적용
    angle_rads[:, 1::2] = np.cos(angle_rads[:, 1::2])

    pos_encoding = angle_rads[np.newaxis, ...]

    return tf.cast(pos_encoding, dtype=float_dtype)


def create_padding_mask(seq):
    # 패딩 부분은 1 아닌 부분은 0으로 변환
    seq = tf.cast(tf.math.equal(seq, 0), float_dtype)
    return seq[:, tf.newaxis, tf.newaxis, :]  # (batch_size, 1, 1, seq_len)


def create_look_ahead_mask(size):
    # look-ahead-mask
    mask = 1 - tf.linalg.band_part(tf.ones((size, size)), -1, 0)
    mask = tf.cast(mask, float_dtype)
    return mask  # (seq_len, seq_len)


def scaled_dot_product_attention(q, k, v, mask):
    # q, k matmul
    qk = tf.matmul(q, k, transpose_b=True)  # (..., seq_len_q, seq_len_k)

    # qk를 sqrt(dk)로 나누기
    dk = tf.cast(tf.shape(k)[-1], float_dtype)
    scaled_attention_logits = qk / tf.math.sqrt(dk)

    # 패딩 부분은 매우 작은 값으로 (attention 연산에서 제외하기 위해)
    if mask is not None:
        scaled_attention_logits += (mask * -1e9)

    attention_weights = tf.nn.softmax(scaled_attention_logits, axis=-1)  # (..., seq_len_q, seq_len_k)

    output = tf.matmul(attention_weights, v)  # (..., seq_len_q, depth_v)

    return output, attention_weights


class MultiHeadAttention(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads):
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads
        self.d_model = d_model

        assert d_model % self.num_heads == 0  # d_model은 num_heads로 나눠 떨어져야 함

        self.depth = d_model // self.num_heads

        self.wq = tf.keras.layers.Dense(d_model)
        self.wk = tf.keras.layers.Dense(d_model)
        self.wv = tf.keras.layers.Dense(d_model)

        self.dense = tf.keras.layers.Dense(d_model)

    def split_heads(self, x, batch_size):
        """
        self.num_heads에 따라 x를 분할한다. (num_heads, depth)
        최종적으로 다음 shape를 반환한다 : (batch_size, num_heads, seq_len, depth)
        """
        x = tf.reshape(x, (batch_size, -1, self.num_heads, self.depth))
        return tf.transpose(x, perm=[0, 2, 1, 3])

    def call(self, v, k, q, mask):
        # Multi-head attention 수행
        batch_size = tf.shape(q)[0]

        q = self.wq(q)  # (batch_size, seq_len, d_model)
        k = self.wk(k)  # (batch_size, seq_len, d_model)
        v = self.wv(v)  # (batch_size, seq_len, d_model)

        q = self.split_heads(q, batch_size)  # (batch_size, num_heads, seq_len_q, depth)
        k = self.split_heads(k, batch_size)  # (batch_size, num_heads, seq_len_k, depth)
        v = self.split_heads(v, batch_size)  # (batch_size, num_heads, seq_len_v, depth)

        # scaled_attention.shape == (batch_size, num_heads, seq_len_q, depth)
        # attention_weights.shape == (batch_size, num_heads, seq_len_q, seq_len_k)
        scaled_attention, attention_weights = scaled_dot_product_attention(
            q, k, v, mask)

        scaled_attention = tf.transpose(scaled_attention,
                                        perm=[0, 2, 1, 3])  # (batch_size, seq_len_q, num_heads, depth)

        # 헤드들을 모두 concatenate
        concat_attention = tf.reshape(scaled_attention,
                                      (batch_size, -1, self.d_model))  # (batch_size, seq_len_q, d_model)

        output = self.dense(concat_attention)  # (batch_size, seq_len_q, d_model)

        return output, attention_weights


def pointwise_ffn(d_model, dff):
    # point wise feed forward neural network
    return tf.keras.Sequential([
        tf.keras.layers.Dense(dff),  # (batch_size, seq_len, dff)
        tf.keras.layers.Activation(gelu),
        tf.keras.layers.Dropout(dropout_rate),
        tf.keras.layers.Dense(d_model)  # (batch_size, seq_len, d_model)
    ])


class EncoderLayer(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, dff, rate=0.1):
        super(EncoderLayer, self).__init__()

        self.mha = MultiHeadAttention(d_model, num_heads)
        self.ffn = pointwise_ffn(d_model, dff)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)

    def call(self, x, training, mask):
        # multi-head attention
        attn_output, _ = self.mha(x, x, x, mask)  # (batch_size, input_seq_len, d_model)
        attn_output = self.dropout1(attn_output, training=training)
        out1 = self.layernorm1(x + attn_output)  # (batch_size, input_seq_len, d_model)

        # ffn
        ffn_output = self.ffn(out1)  # (batch_size, input_seq_len, d_model)
        ffn_output = self.dropout2(ffn_output, training=training)
        out2 = self.layernorm2(out1 + ffn_output)  # (batch_size, input_seq_len, d_model)

        return out2


class DecoderLayer(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, dff, rate=0.1):
        super(DecoderLayer, self).__init__()

        self.mha1 = MultiHeadAttention(d_model, num_heads)
        self.mha2 = MultiHeadAttention(d_model, num_heads)

        self.ffn = pointwise_ffn(d_model, dff)

        self.layernorm1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.layernorm3 = tf.keras.layers.LayerNormalization(epsilon=1e-6)

        self.dropout1 = tf.keras.layers.Dropout(rate)
        self.dropout2 = tf.keras.layers.Dropout(rate)
        self.dropout3 = tf.keras.layers.Dropout(rate)

    def call(self, x, enc_output, training, look_ahead_mask, padding_mask):
        # enc_output.shape == (batch_size, input_seq_len, d_model)

        # masked multi-head attention
        attn1, attn_weights_block1 = self.mha1(x, x, x, look_ahead_mask)  # (batch_size, target_seq_len, d_model)
        attn1 = self.dropout1(attn1, training=training)
        out1 = self.layernorm1(attn1 + x)

        # multi-head attention
        attn2, attn_weights_block2 = self.mha2(
            enc_output, enc_output, out1, padding_mask)  # (batch_size, target_seq_len, d_model)
        attn2 = self.dropout2(attn2, training=training)
        out2 = self.layernorm2(attn2 + out1)  # (batch_size, target_seq_len, d_model)

        # ffn
        ffn_output = self.ffn(out2)  # (batch_size, target_seq_len, d_model)
        ffn_output = self.dropout3(ffn_output, training=training)
        out3 = self.layernorm3(ffn_output + out2)  # (batch_size, target_seq_len, d_model)

        return out3, attn_weights_block1, attn_weights_block2


class Encoder(tf.keras.layers.Layer):
    def __init__(
        self, num_layers, d_model, num_heads, dff, input_vocab_size,
        maximum_position_encoding, rate=0.1
    ):
        super(Encoder, self).__init__()

        self.d_model = d_model
        self.num_layers = num_layers

        self.embedding = tf.keras.layers.Embedding(input_vocab_size, d_model)
        #self.embedding = elmo_embedding
        self.pos_encoding = positional_encoding(maximum_position_encoding,
                                                self.d_model)

        self.enc_layers = [EncoderLayer(d_model, num_heads, dff, rate)
                           for _ in range(num_layers)]

        self.dropout = tf.keras.layers.Dropout(rate)

    def call(self, x, training, mask):
        seq_len = tf.shape(x)[1]

        # embedding
        x = self.embedding(x)  # (batch_size, input_seq_len, d_model)

        x *= tf.math.sqrt(tf.cast(self.d_model, float_dtype))
        # positional encoding
        x += self.pos_encoding[:, :seq_len, :]

        x = self.dropout(x, training=training)

        for i in range(self.num_layers):
            # encoder layer
            x = self.enc_layers[i](x, training, mask)

        return x  # (batch_size, input_seq_len, d_model)


class Decoder(tf.keras.layers.Layer):
    def __init__(
        self, num_layers, d_model, num_heads, dff, target_vocab_size,
        maximum_position_encoding, rate=0.1
    ):
        super(Decoder, self).__init__()

        self.d_model = d_model
        self.num_layers = num_layers

        self.embedding = tf.keras.layers.Embedding(target_vocab_size, d_model)
        #self.embedding = elmo_embedding
        self.pos_encoding = positional_encoding(maximum_position_encoding, d_model)

        self.dec_layers = [DecoderLayer(d_model, num_heads, dff, rate)
                           for _ in range(num_layers)]
        self.dropout = tf.keras.layers.Dropout(rate)

    def call(self, x, enc_output, training, look_ahead_mask, padding_mask):
        seq_len = tf.shape(x)[1]
        attention_weights = {}

        # embedding
        x = self.embedding(x)  # (batch_size, target_seq_len, d_model)
        x *= tf.math.sqrt(tf.cast(self.d_model, float_dtype))
        # positional encoding
        x += self.pos_encoding[:, :seq_len, :]

        x = self.dropout(x, training=training)

        for i in range(self.num_layers):
            # decoder layer
            x, block1, block2 = self.dec_layers[i](x, enc_output, training,
                                                   look_ahead_mask, padding_mask)

            attention_weights['decoder_layer{}_block1'.format(i + 1)] = block1
            attention_weights['decoder_layer{}_block2'.format(i + 1)] = block2

        # x.shape == (batch_size, target_seq_len, d_model)
        return x, attention_weights


class Transformer(tf.keras.Model):
    def __init__(
        self, num_layers, d_model, num_heads, dff, input_vocab_size,
        target_vocab_size, pe_input, pe_target, rate=0.1
    ):
        super(Transformer, self).__init__()

        # 인코더, 디코더
        self.encoder = Encoder(
            num_layers, d_model, num_heads, dff,
            input_vocab_size, pe_input, rate
        )
        self.decoder = Decoder(
            num_layers, d_model, num_heads, dff,
            target_vocab_size, pe_target, rate
        )
        # 최종 레이어
        self.final_layer = tf.keras.layers.Dense(target_vocab_size)

        # tokenizer
        self.tk = joblib.load('./korean_polisher/assets/tokenizer/tokenizer.joblib')

        # optimizer
        self.learning_rate = CustomSchedule(200000)
        self.optimizer = tf.keras.optimizers.Adam(self.learning_rate, beta_1=0.9, beta_2=0.98, epsilon=1e-9)

        # checkpoint
        self.ckpt = tf.train.Checkpoint(
            transformer=self,
            optimizer=self.optimizer
        )
        self.ckpt_manager = tf.train.CheckpointManager(self.ckpt, checkpoint_path, max_to_keep=5)

    def call(self, inp, tar, training, enc_padding_mask, look_ahead_mask, dec_padding_mask):
        # encoder 연산
        enc_output = self.encoder(inp, training, enc_padding_mask)  # (batch_size, inp_seq_len, d_model)

        # decoder 연산
        # dec_output.shape == (batch_size, tar_seq_len, d_model)
        dec_output, attention_weights = self.decoder(
            tar, enc_output, training, look_ahead_mask, dec_padding_mask)

        # 최종 레이어 연산
        final_output = self.final_layer(dec_output)  # (batch_size, tar_seq_len, target_vocab_size)

        return final_output, attention_weights
    
    @tf.function(input_signature=train_step_signature)
    def train_step(self, inp, tar):
        tar_inp = tar[:, :-1]
        tar_real = tar[:, 1:]

        enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)

        with tf.GradientTape() as tape:
            predictions, _ = self(
                inp, tar_inp,
                True,
                enc_padding_mask,
                combined_mask,
                dec_padding_mask
            )

            loss = loss_function(tar_real, predictions)

        gradients = tape.gradient(loss, self.trainable_variables)
        self.optimizer.apply_gradients(zip(gradients, self.trainable_variables))

        train_loss(loss)
        train_accuracy(tar_real, predictions)

    def predict(self, inp_sentence):
        """input 텍스트 예측"""

        # 인코딩 (토크나이징)
        encoder_input = tokenize_batch([[inp_sentence]], self.tk)
        encoder_input = tf.cast(encoder_input, int_dtype)

        # 디코더 input
        decoder_input = [2]  # [CLS] token
        output = tf.expand_dims(decoder_input, 0)
        output = tf.cast(output, int_dtype)
        result = decoder_input.copy()

        for i in range(MAX_LENGTH):
            enc_padding_mask, combined_mask, dec_padding_mask = create_masks(
                encoder_input, output)

            # predictions.shape == (batch_size, seq_len, vocab_size)
            predictions, attention_weights = \
                self(
                    encoder_input,
                    output,
                    False,
                    enc_padding_mask,
                    combined_mask,
                    dec_padding_mask
                )

            # 가장 마지막 단어만
            predictions = predictions[:, -1:, :]  # (batch_size, 1, vocab_size)

            predicted_id = tf.cast(tf.argmax(predictions, axis=-1), int_dtype)
            output = tf.concat([output, predicted_id], axis=-1)
            predicted_id = predicted_id.numpy()[0][0]

            if predicted_id == 3:
                # [SEP] token이라면? -> 문장 끝
                return self.tk.decode(result)

            # 예측 결과 합치기
            result.append(predicted_id)

        return self.tk.decode(result)

    def evaluate(self, inp, tar):
        """test loss, acc 계산"""

        def split_batch(iterable, n=1):
            # data를 batch 크기로 slice
            l = len(iterable)
            for ndx in range(0, l, n):
                yield iterable[ndx:min(ndx + n, l)]
        batch_size = BATCH_SIZE  # validation batch
        inp_batch = split_batch(inp, batch_size)
        tar_batch = split_batch(tar, batch_size)

        test_loss = tf.keras.metrics.Mean(name='test_loss')
        test_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='test_accuracy')
        test_loss.reset_states()
        test_accuracy.reset_states()

        for inp, tar in zip(inp_batch, tar_batch):
            tar_inp = tar[:, :-1]
            tar_real = tar[:, 1:]

            enc_padding_mask, combined_mask, dec_padding_mask = create_masks(inp, tar_inp)
            predictions, _ = self(inp, tar_inp,
                                        False,
                                        enc_padding_mask,
                                        combined_mask,
                                        dec_padding_mask)
            loss = loss_function(tar_real, predictions)

            test_loss(loss)
            test_accuracy(tar_real, predictions)

        return test_loss.result().numpy(), test_accuracy.result().numpy()

    def demo(self):
        """'demo.txt'의 텍스트를 예측하여 출력"""

        try:
            with open('./demo.txt', 'r', encoding='utf8') as f:
                d = f.read()
            for i in d.split('\n'):
                if not len(i) == 0:
                    print(i)
                    print(self.predict(i))
        except Exception as e:
            print("demo error")
            print(e)
    
    def ckpt_save(self, epoch, batch_iter):
        """체크포인트 저장 (epoch, batch_iter도 저장)"""

        if not os.path.isdir(checkpoint_path):
            os.mkdir(checkpoint_path)
        with open(f"{checkpoint_path}/latest_epoch.txt", 'w') as f:
            f.write(str(epoch))
        with open(f"{checkpoint_path}/latest_batch_iter.txt", 'w') as f:
            f.write(str(batch_iter))
        
        return self.ckpt_manager.save()

    def _history(self, test_loss, test_acc):
        with open('./history.txt', 'a+') as f:
            f.write('\n%s %s' % (test_loss, test_acc))


def create_masks(inp, tar):
    # padding mask : 어텐션 연산에서 패딩을 제외시킬 때 사용
    # Encoder에서 사용
    enc_padding_mask = create_padding_mask(inp)

    # Decoder 두 번째 블록에서 사용
    dec_padding_mask = create_padding_mask(inp)

    # look ahead mask : 미래의 단어를 볼 수 없도록 가릴 때 사용
    # Decoder 첫 번째 블록에서 사용
    look_ahead_mask = create_look_ahead_mask(tf.shape(tar)[1])
    dec_target_padding_mask = create_padding_mask(tar)
    combined_mask = tf.maximum(dec_target_padding_mask, look_ahead_mask)

    return enc_padding_mask, combined_mask, dec_padding_mask


def loss_function(real, pred):
    # loss object
    loss_object = tf.keras.losses.SparseCategoricalCrossentropy(
        from_logits=True, reduction='none')

    mask = tf.math.logical_not(tf.math.equal(real, 0))
    loss_ = loss_object(real, pred)

    # padding 부분은 loss에 반영하지 않음
    mask = tf.cast(mask, dtype=loss_.dtype)
    loss_ *= mask

    return tf.reduce_sum(loss_) / tf.reduce_sum(mask)


train_loss = tf.keras.metrics.Mean(name='train_loss')
train_accuracy = tf.keras.metrics.SparseCategoricalAccuracy(name='train_accuracy')
