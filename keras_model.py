import tensorflow as tf
from tensorflow.python import keras
from tensorflow.python.keras.layers import Input, Embedding, Concatenate, Dropout, TimeDistributed, Dense
import tensorflow.python.keras.backend as K
from tensorflow.python.keras.callbacks import Callback
from tensorflow.python.keras.metrics import sparse_top_k_categorical_accuracy

from path_context_reader import PathContextReader, ModelInputTensorsFormer, ReaderInputTensors
import os
import numpy as np
from functools import partial
from typing import List, Optional
from vocabularies import SpecialVocabWords, VocabType
from keras_attention_layer import AttentionLayer
from keras_word_prediction_layer import WordPredictionLayer
from keras_words_subtoken_metrics import WordsSubtokenPrecisionMetric, WordsSubtokenRecallMetric, WordsSubtokenF1Metric
from config import Config
from model_base import Code2VecModelBase, ModelEvaluationResults, EstimatorMode


class ModelCheckpointSaverCallback(Callback):
    def __init__(self, code2vec_model: 'Code2VecModel'):
        self.code2vec_model = code2vec_model
        self.last_saved_epoch = code2vec_model.nr_epochs_trained
        super(ModelCheckpointSaverCallback, self).__init__()

    def on_epoch_end(self, epoch, logs=None):
        assert self.code2vec_model.nr_epochs_trained == epoch
        self.code2vec_model.nr_epochs_trained += 1
        nr_non_saved_epochs = self.code2vec_model.nr_epochs_trained - self.last_saved_epoch
        if nr_non_saved_epochs >= self.code2vec_model.config.SAVE_EVERY_EPOCHS:
            self.code2vec_model.save()
            self.last_saved_epoch = self.code2vec_model.nr_epochs_trained


class _KerasModelInputTensorsFormer(ModelInputTensorsFormer):
    def __init__(self, estimator_mode: EstimatorMode):
        self.estimator_mode = estimator_mode

    def to_model_input_form(self, input_tensors: ReaderInputTensors):
        inputs = (input_tensors.path_source_token_indices, input_tensors.path_indices,
                  input_tensors.path_target_token_indices, input_tensors.context_valid_mask)
        if self.estimator_mode is EstimatorMode.Predict:
            return inputs
        targets = {'y_hat': input_tensors.target_index}
        if self.estimator_mode is EstimatorMode.Evaluate:
            targets['true_target_word'] = input_tensors.target_string
        return inputs, targets

    def from_model_input_form(self, input_row) -> ReaderInputTensors:
        target_index, target_string = None, None
        if self.estimator_mode is EstimatorMode.Predict:
            inputs = input_row
        else:
            inputs = input_row[0]
            targets = input_row[1]
            target_index = targets['y_hat']
            target_string = (targets['true_target_word'] if self.estimator_mode is EstimatorMode.Evaluate else None)
        return ReaderInputTensors(
            path_source_token_indices=inputs[0],
            path_indices=inputs[1],
            path_target_token_indices=inputs[2],
            context_valid_mask=inputs[3],
            target_index=target_index,
            target_string=target_string
        )


class Code2VecModel(Code2VecModelBase):
    def __init__(self, config: Config):
        self.keras_model: Optional[keras.Model] = None
        self.nr_epochs_trained: int = 0
        self._checkpoint: Optional[tf.train.Checkpoint] = None
        self._save_checkpoint_manager: Optional[tf.train.CheckpointManager] = None
        super(Code2VecModel, self).__init__(config)

    def _create_keras_model(self):
        # Each input sample consists of a bag of x`MAX_CONTEXTS` tuples (source_terminal, path, target_terminal).
        # The valid mask indicates for each context whether it actually exists or it is just a padding.
        path_source_token_input = Input((self.config.MAX_CONTEXTS,), dtype=tf.int32)
        path_input = Input((self.config.MAX_CONTEXTS,), dtype=tf.int32)
        path_target_token_input = Input((self.config.MAX_CONTEXTS,), dtype=tf.int32)
        context_valid_mask = Input((self.config.MAX_CONTEXTS,))

        # TODO: consider set embedding initializer or leave it default? [for 2 embeddings below and last dense layer]

        # Input paths are indexes, we embed these here.
        paths_embedded = Embedding(
            self.vocabs.path_vocab.size, self.config.PATH_EMBEDDINGS_SIZE, name='path_embedding')(path_input)

        # Input terminals are indexes, we embed these here.
        token_embedding_shared_layer = Embedding(
            self.vocabs.token_vocab.size, self.config.TOKEN_EMBEDDINGS_SIZE, name='token_embedding')
        path_source_token_embedded = token_embedding_shared_layer(path_source_token_input)
        path_target_token_embedded = token_embedding_shared_layer(path_target_token_input)

        # `Context` is a concatenation of the 2 terminals & path embedding.
        # Each context is a vector of size 3 * EMBEDDINGS_SIZE.
        context_embedded = Concatenate()([path_source_token_embedded, paths_embedded, path_target_token_embedded])
        context_embedded = Dropout(1 - self.config.DROPOUT_KEEP_RATE)(context_embedded)

        # Lets get dense: Apply a dense layer for each context vector (using same weights for all of the context).
        context_after_dense = TimeDistributed(
            Dense(self.config.CODE_VECTOR_SIZE, use_bias=False, activation='tanh'))(context_embedded)

        # The final code vectors are received by applying attention to the "densed" context vectors.
        code_vectors = AttentionLayer(name='code_vectors')(context_after_dense, mask=context_valid_mask)

        # "Decode": Now we use another dense layer to get the target word embedding from each code vector.
        y_hat = Dense(self.vocabs.target_vocab.size, use_bias=False, activation='softmax', name='y_hat')(code_vectors)

        # Actual target word prediction (as string). Used as a second output layer.
        # `predict()` method just have to return the output of this layer.
        # Also used for the evaluation metrics calculations.
        target_word_prediction = WordPredictionLayer(
            self.config.TOP_K_WORDS_CONSIDERED_DURING_PREDICTION,
            self.vocabs.target_vocab.get_index_to_word_lookup_table(),
            predicted_words_filters=[
                lambda word_indices, _: tf.not_equal(word_indices, self.vocabs.target_vocab.word_to_index[SpecialVocabWords.OOV]),
                lambda _, word_strings: tf.strings.regex_full_match(word_strings, r'^[a-zA-Z\|]+$')
            ], name='target_word_prediction')(y_hat)

        # Wrap the layers into a Keras model, using our subtoken-metrics and the CE loss.
        keras_model = keras.Model(
            inputs=[path_source_token_input, path_input, path_target_token_input, context_valid_mask],
            outputs=[y_hat, code_vectors, target_word_prediction])

        self.keras_model = keras_model

    @property
    def target_word_prediction_layer_output(self):
        return self.keras_model.get_layer('target_word_prediction').output

    def _create_metrics_for_keras_model(self) -> List[keras.metrics.Metric]:
        top_k_acc_metric = partial(
            sparse_top_k_categorical_accuracy, k=self.config.TOP_K_WORDS_CONSIDERED_DURING_PREDICTION)
        top_k_acc_metric.__name__ = 'top{k}_acc'.format(k=self.config.TOP_K_WORDS_CONSIDERED_DURING_PREDICTION)
        words_subtoken_metrics_kwargs = {
            'index_to_word_table': self.vocabs.target_vocab.get_index_to_word_lookup_table(),
            'predicted_word_output': self.target_word_prediction_layer_output
        }
        metrics = [
            top_k_acc_metric,
            WordsSubtokenPrecisionMetric(**words_subtoken_metrics_kwargs, name='subtoken_precision'),
            WordsSubtokenRecallMetric(**words_subtoken_metrics_kwargs, name='subtoken_recall'),
            WordsSubtokenF1Metric(**words_subtoken_metrics_kwargs, name='subtoken_f1')
        ]
        return metrics

    @classmethod
    def _create_optimizer(cls):
        return tf.train.AdamOptimizer()

    def _compile_keras_model(self, optimizer=None):
        if optimizer is None:
            optimizer = self._create_optimizer()
        self.keras_model.compile(
            loss={'y_hat': 'sparse_categorical_crossentropy'},
            optimizer=optimizer,
            metrics={'y_hat': self._create_metrics_for_keras_model()})

    def _create_data_reader(self, estimator_mode: EstimatorMode, repeat_endlessly: bool = False):
        return PathContextReader(
            vocabs=self.vocabs,
            config=self.config,
            model_input_tensors_former=_KerasModelInputTensorsFormer(estimator_mode=estimator_mode),
            is_evaluating=(estimator_mode is EstimatorMode.Evaluate),
            repeat_endlessly=repeat_endlessly)

    def train(self):
        # initialize the input pipeline readers
        train_data_input_reader = self._create_data_reader(estimator_mode=EstimatorMode.Train)
        val_data_input_reader = self._create_data_reader(estimator_mode=EstimatorMode.Evaluate, repeat_endlessly=True)

        # TODO: do we want to use early stopping? if so, use the right chechpoint manager and set the correct
        #       `monitor` quantity (example: monitor='val_acc', mode='max')

        self.keras_model.fit(
            train_data_input_reader.dataset,
            steps_per_epoch=self.config.train_steps_per_epoch,
            epochs=self.config.NUM_EPOCHS,
            initial_epoch=self.nr_epochs_trained,
            batch_size=self.config.TRAIN_BATCH_SIZE,
            validation_data=val_data_input_reader.dataset,
            validation_steps=self.config.test_steps_per_epoch,
            callbacks=[ModelCheckpointSaverCallback(self)])

    def evaluate(self) -> Optional[ModelEvaluationResults]:
        val_data_input_reader = self._create_data_reader(estimator_mode=EstimatorMode.Evaluate)
        eval_res = self.keras_model.evaluate(val_data_input_reader.dataset, steps=self.config.test_steps_per_epoch)
        return ModelEvaluationResults(
            topk_acc=eval_res[2],
            subtoken_precision=eval_res[3],
            subtoken_recall=eval_res[4],
            subtoken_f1=eval_res[5],
            loss=eval_res[0]
        )

    def predict(self, predict_data_lines):
        # TODO: consider using `tf.data.Dataset.from_tensor_slices()` instead of placeholders.
        predict_placeholder = tf.placeholder(tf.string)
        predict_input_reader = self._create_data_reader(estimator_mode=EstimatorMode.Predict)
        reader_output = predict_input_reader.process_input_from_row_placeholder(predict_placeholder)

        for predict_line in predict_data_lines:
            print('type(predict_line)', type(predict_line))
            print('predict_line', predict_line)

            line_after_input_processing = K.get_session().run(
                reader_output, feed_dict={predict_placeholder: predict_line})
            print(type(line_after_input_processing))
            print(line_after_input_processing)
            print([a.shape for a in line_after_input_processing])
            prediction_results = self.keras_model.predict(line_after_input_processing)
            print('prediction_results', prediction_results)
            # TODO: impl the rest
        return

    def _save_inner_model(self, path):
        if self.config.RELEASE:
            self.keras_model.save_weights(self.config.get_model_weights_path(path))
        else:
            with K.get_session().as_default():
                self._get_save_checkpoint_manager().save(checkpoint_number=self.nr_epochs_trained)

    def _create_inner_model(self):
        self._create_keras_model()
        self._compile_keras_model()
        self.keras_model.summary()

    def _load_inner_model(self):
        self._create_keras_model()
        self._compile_keras_model()

        # when loading the model for further training, we must use the full saved model file (not just weights).
        must_use_full_model = self.config.TRAIN_DATA_PATH_PREFIX
        if must_use_full_model and not os.path.exists(self.config.full_model_load_path):
            raise ValueError(
                "There is no model at path `{model_file_path}`. When loading the model for further training, "
                "we must use a full saved model file (not just weights).".format(
                    model_file_path=self.config.full_model_load_path))
        use_full_model = must_use_full_model or not os.path.exists(self.config.model_weights_load_path)

        if use_full_model:
            latest_checkpoint = tf.train.latest_checkpoint(self.config.full_model_load_path)
            print('Loading latest checkpoint `{}`.'.format(latest_checkpoint))
            status = self._get_checkpoint().restore(tf.train.latest_checkpoint(self.config.full_model_load_path))
            status.initialize_or_restore(K.get_session())
            self._compile_keras_model()  # We have to re-compile because we also recovered the `tf.train.AdamOptimizer`.
            self.nr_epochs_trained = int(latest_checkpoint.split('-')[-1])
        else:
            # load the "released" model (only the weights).
            self.keras_model.load_weights(self.config.model_weights_load_path)

        self.keras_model.summary()

    def _get_checkpoint(self):
        assert self.keras_model is not None and self.keras_model.optimizer is not None
        if self._checkpoint is None:
            self._checkpoint = tf.train.Checkpoint(optimizer=self.keras_model.optimizer, model=self.keras_model)
        return self._checkpoint

    def _get_save_checkpoint_manager(self):
        if self._save_checkpoint_manager is None:
            self._save_checkpoint_manager = tf.train.CheckpointManager(
                self._get_checkpoint(), self.config.full_model_save_path, max_to_keep=self.config.MAX_TO_KEEP)
        return self._save_checkpoint_manager

    def _get_vocab_embedding_as_np_array(self, vocab_type: VocabType) -> np.ndarray:
        assert vocab_type in VocabType

        vocab_type_to_embedding_layer_mapping = {
            VocabType.Target: 'y_hat',
            VocabType.Token: 'token_embedding',
            VocabType.Path: 'path_embedding'
        }
        embedding_layer_name = vocab_type_to_embedding_layer_mapping[vocab_type]
        weight = np.array(self.keras_model.get_layer(embedding_layer_name).get_weights()[0])
        assert len(weight.shape) == 2

        # token, path have an actual `Embedding` layers, but target have just a `Dense` layer.
        # hence, transpose the weight when necessary.
        assert self.vocabs.get(vocab_type).size in weight.shape
        if self.vocabs.get(vocab_type).size != weight.shape[0]:
            weight = np.transpose(weight)

        return weight

    def _initialize_tables(self):
        PathContextReader.create_needed_vocabs_lookup_tables(self.vocabs)
        K.get_session().run(tf.tables_initializer())
        print('Initalized tables')

    def _initialize(self):
        self._initialize_tables()
