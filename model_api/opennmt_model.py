# from model_api.abstract_model_api import AbstractModelAPI
from __future__ import division, unicode_literals

import argparse
import io
from itertools import chain

import h5py
import numpy as np
import onmt
import onmt.ModelConstructor
import onmt.io
import onmt.modules
import onmt.translate
import torch
from onmt.io import TextDataset

PAD_WORD = '<blank>'
UNK = 0
BOS_WORD = '<s>'
EOS_WORD = '</s>'


def translate_opts(parser):
    group = parser.add_argument_group('Model')
    group.add_argument('-model', required=False,
                       help='Path to model .pt file')

    group = parser.add_argument_group('Data')
    group.add_argument('-data_type', default="text",
                       help="Type of the source input. Options: [text|img].")
    group.add_argument('-src_dir', default="",
                       help='Source directory for image or audio files')
    group.add_argument('-tgt',
                       help='True target sequence (optional)')
    group.add_argument('-output', default='pred.txt',
                       help="""Path to output the predictions (each line will
                       be the decoded sequence""")

    # Options most relevant to summarization.
    group.add_argument('-dynamic_dict', action='store_true',
                       help="Create dynamic dictionaries")
    group.add_argument('-share_vocab', action='store_true',
                       help="Share source and target vocabulary")

    group = parser.add_argument_group('Beam')
    group.add_argument('-beam_size', type=int, default=5,
                       help='Beam size')

    # Alpha and Beta values for Google Length + Coverage penalty
    # Described here: https://arxiv.org/pdf/1609.08144.pdf, Section 7
    group.add_argument('-alpha', type=float, default=0.,
                       help="""Google NMT length penalty parameter
                        (higher = longer generation)""")
    group.add_argument('-beta', type=float, default=-0.,
                       help="""Coverage penalty parameter""")
    group.add_argument('-max_sent_length', type=int, default=100,
                       help='Maximum sentence length.')
    group.add_argument('-replace_unk', action="store_true",
                       help="""Replace the generated UNK tokens with the
                       source token that had highest attention weight. If
                       phrase_table is provided, it will lookup the
                       identified source token and give the corresponding
                       target token. If it is not provided(or the identified
                       source token does not exist in the table) then it
                       will copy the source token""")

    group = parser.add_argument_group('Logging')
    group.add_argument('-verbose', action="store_true",
                       help='Print scores and predictions for each sentence')
    group.add_argument('-attn_debug', action="store_true",
                       help='Print best attn for each word')
    group.add_argument('-dump_beam', type=str, default="",
                       help='File to dump beam information to.')
    group.add_argument('-n_best', type=int, default=1,
                       help="""If verbose is set, will output the n_best
                       decoded sentences""")

    group = parser.add_argument_group('Efficiency')
    group.add_argument('-batch_size', type=int, default=30,
                       help='Batch size')
    group.add_argument('-gpu', type=int, default=-1,
                       help="Device to run on")

    # Options most relevant to speech.
    group = parser.add_argument_group('Speech')
    group.add_argument('-sample_rate', type=int, default=16000,
                       help="Sample rate.")
    group.add_argument('-window_size', type=float, default=.02,
                       help='Window size for spectrogram in seconds')
    group.add_argument('-window_stride', type=float, default=.01,
                       help='Window stride for spectrogram in seconds')
    group.add_argument('-window', default='hamming',
                       help='Window type for spectrogram generation')


def model_opts(parser):
    """
    These options are passed to the construction of the model.
    Be careful with these as they will be used during translation.
    """

    # Embedding Options
    group = parser.add_argument_group('Model-Embeddings')
    group.add_argument('-src_word_vec_size', type=int, default=500,
                       help='Word embedding size for src.')
    group.add_argument('-tgt_word_vec_size', type=int, default=500,
                       help='Word embedding size for tgt.')
    group.add_argument('-word_vec_size', type=int, default=-1,
                       help='Word embedding size for src and tgt.')

    group.add_argument('-share_decoder_embeddings', action='store_true',
                       help="""Use a shared weight matrix for the input and
                       output word  embeddings in the decoder.""")
    group.add_argument('-share_embeddings', action='store_true',
                       help="""Share the word embeddings between encoder
                       and decoder. Need to use shared dictionary for this
                       option.""")
    group.add_argument('-position_encoding', action='store_true',
                       help="""Use a sin to mark relative words positions.
                       Necessary for non-RNN style models.
                       """)

    group = parser.add_argument_group('Model-Embedding Features')
    group.add_argument('-feat_merge', type=str, default='concat',
                       choices=['concat', 'sum', 'mlp'],
                       help="""Merge action for incorporating features embeddings.
                       Options [concat|sum|mlp].""")
    group.add_argument('-feat_vec_size', type=int, default=-1,
                       help="""If specified, feature embedding sizes
                       will be set to this. Otherwise, feat_vec_exponent
                       will be used.""")
    group.add_argument('-feat_vec_exponent', type=float, default=0.7,
                       help="""If -feat_merge_size is not set, feature
                       embedding sizes will be set to N^feat_vec_exponent
                       where N is the number of values the feature takes.""")

    # Encoder-Deocder Options
    group = parser.add_argument_group('Model- Encoder-Decoder')
    group.add_argument('-model_type', default='text',
                       help="""Type of source model to use. Allows
                       the system to incorporate non-text inputs.
                       Options are [text|img|audio].""")

    group.add_argument('-encoder_type', type=str, default='rnn',
                       choices=['rnn', 'brnn', 'mean', 'transformer', 'cnn'],
                       help="""Type of encoder layer to use. Non-RNN layers
                       are experimental. Options are
                       [rnn|brnn|mean|transformer|cnn].""")
    group.add_argument('-decoder_type', type=str, default='rnn',
                       choices=['rnn', 'transformer', 'cnn'],
                       help="""Type of decoder layer to use. Non-RNN layers
                       are experimental. Options are
                       [rnn|transformer|cnn].""")

    group.add_argument('-layers', type=int, default=-1,
                       help='Number of layers in enc/dec.')
    group.add_argument('-enc_layers', type=int, default=2,
                       help='Number of layers in the encoder')
    group.add_argument('-dec_layers', type=int, default=2,
                       help='Number of layers in the decoder')
    group.add_argument('-rnn_size', type=int, default=500,
                       help='Size of rnn hidden states')
    group.add_argument('-cnn_kernel_width', type=int, default=3,
                       help="""Size of windows in the cnn, the kernel_size is
                       (cnn_kernel_width, 1) in conv layer""")

    group.add_argument('-input_feed', type=int, default=1,
                       help="""Feed the context vector at each time step as
                       additional input (via concatenation with the word
                       embeddings) to the decoder.""")

    group.add_argument('-rnn_type', type=str, default='LSTM',
                       choices=['LSTM', 'GRU'],
                       help="""The gate type to use in the RNNs""")
    # group.add_argument('-residual',   action="store_true",
    #                     help="Add residual connections between RNN layers.")
    group.add_argument('-brnn_merge', default='concat',
                       choices=['concat', 'sum'],
                       help="Merge action for the bidir hidden states")

    group.add_argument('-context_gate', type=str, default=None,
                       choices=['source', 'target', 'both'],
                       help="""Type of context gate to use.
                       Do not select for no context gate.""")

    # Attention options
    group = parser.add_argument_group('Model- Attention')
    group.add_argument('-global_attention', type=str, default='general',
                       choices=['dot', 'general', 'mlp'],
                       help="""The attention type to use:
                       dotprod or general (Luong) or MLP (Bahdanau)""")

    # Genenerator and loss options.
    group.add_argument('-copy_attn', action="store_true",
                       help='Train copy attention layer.')
    group.add_argument('-copy_attn_force', action="store_true",
                       help='When available, train to copy.')
    group.add_argument('-coverage_attn', action="store_true",
                       help='Train a coverage attention layer.')
    group.add_argument('-lambda_coverage', type=float, default=1,
                       help='Lambda value for coverage.')


class ONMTmodelAPI():
    def __init__(self, model_loc, gpu=-1, beam_size=5, k=5):
        # Simulate all commandline args
        parser = argparse.ArgumentParser(
            description='translate.py',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)
        translate_opts(parser)
        self.opt = parser.parse_known_args()[0]
        self.opt.model = model_loc
        self.opt.beam_size = beam_size
        self.opt.batch_size = 1
        self.opt.n_best = k

        dummy_parser = argparse.ArgumentParser(description='train.py')
        model_opts(dummy_parser)
        self.dummy_opt = dummy_parser.parse_known_args([])[0]

        # Load the model.
        self.fields, self.model, self.model_opt = \
            onmt.ModelConstructor.load_test_model(
                self.opt, self.dummy_opt.__dict__)

        # Make GPU decoding possible
        self.opt.gpu = gpu
        self.opt.cuda = self.opt.gpu > -1
        if self.opt.cuda:
            torch.cuda.set_device(self.opt.gpu)

        # Translator
        self.scorer = onmt.translate.GNMTGlobalScorer(
            self.opt.alpha,
            self.opt.beta)
        self.translator = onmt.translate.Translator(
            self.model, self.fields,
            beam_size=self.opt.beam_size,
            n_best=self.opt.n_best,
            global_scorer=self.scorer,
            max_length=self.opt.max_sent_length,
            copy_attn=self.model_opt.copy_attn,
            cuda=self.opt.cuda,
            beam_trace=self.opt.dump_beam != "")

    def translate(self, in_text, partial_decode=[], attn_overwrite=[], k=5,
                  attn=None, dump_data=False, roundTo=5):
        """
        in_text: list of strings
        partial_decode: list of strings, not implemented yet
        k: int, number of top translations to return
        attn: list, not implemented yet
        """

        # Set batch size to number of requested translations
        self.opt.batch_size = len(in_text)
        # Workaround until we have API that does not require files
        # with codecs.open("tmp.txt", "w", "utf-8") as f:
        #     for line in in_text:
        #         f.write(line + "\n")

        if dump_data:
            # Code to extract the source and target dict
            with open("s2s/src.dict", 'w') as f:
                for w, ix in self.translator.fields['src'].vocab.stoi.items():
                    f.write(str(ix) + " " + w + "\n")
            with open("s2s/tgt.dict", 'w') as f:
                for w, ix in self.translator.fields['tgt'].vocab.stoi.items():
                    f.write(str(ix) + " " + w + "\n")
            with h5py.File("s2s/embs.h5", 'w') as f:
                f.create_dataset("encoder", data=
                self.translator.model.encoder.embeddings.emb_luts[
                    0].weight.data.numpy())
                f.create_dataset("decoder", data=
                self.translator.model.decoder.embeddings.emb_luts[
                    0].weight.data.numpy())

        # Use written file as input to dataset builder
        # data = onmt.io.build_dataset(
        #     self.fields, self.opt.data_type,
        #     "tmp.txt", self.opt.tgt,
        #     src_dir=self.opt.src_dir,
        #     sample_rate=self.opt.sample_rate,
        #     window_size=self.opt.window_size,
        #     window_stride=self.opt.window_stride,
        #     window=self.opt.window,
        #     use_filter_pred=False)

        (src_examples_iter, num_src_feats) = \
            ONMTmodelAPI.make_text_examples_nfeats_tpl('\n'.join(in_text), 0,
                                                       'src')

        data = TextDataset(self.fields, src_examples_iter, None,
                           num_src_feats, 0,
                           src_seq_length=0,
                           tgt_seq_length=0,
                           dynamic_dict=True,
                           use_filter_pred=False)

        # Iterating over the single batch... torchtext requirement
        test_data = onmt.io.OrderedIterator(
            dataset=data, device=self.opt.gpu,
            batch_size=self.opt.batch_size, train=False, sort=False,
            sort_within_batch=True,
            shuffle=False)

        # set n_best in translator
        self.translator.n_best = k

        # Increase Beam size if asked for large k
        if self.translator.beam_size < k:
            self.translator.beam_size = k

        # Builder used to convert translation to text
        builder = onmt.translate.TranslationBuilder(
            data, self.translator.fields,
            self.opt.n_best, self.opt.replace_unk, self.opt.tgt)

        # Convert partial decode into valid input to decoder
        print("partial:", partial_decode)
        vocab = self.fields["tgt"].vocab
        partial = []
        for p in partial_decode:
            curr_part = []
            for tok in p.split():
                curr_part.append(vocab.stoi[tok])
            partial.append(curr_part)

        reply = {}

        # Only has one batch, but indexing does not work
        for batch in test_data:
            print(attn_overwrite, 'over')
            batch_data = self.translator.translate_batch(
                batch, data, return_states=True,
                partial=partial, attn_overwrite=attn_overwrite)
            translations = builder.from_batch(batch_data)
            # iteratres over items in batch
            rr = lambda x: [(round(xx, roundTo)) for xx in x]
            for transIx, trans in enumerate(translations):
                context = batch_data['context'][:, transIx, :]
                print(trans.pred_sents)
                res = {}
                # Fill encoder Result
                encoderRes = []
                for token, state in zip(in_text[transIx].split(), context):
                    encoderRes.append({'token': token,
                                       'state': rr(list(state.data))
                                       })
                res['encoder'] = encoderRes

                # # Fill decoder Result
                decoderRes = []
                attnRes = []
                for ix, p in enumerate(trans.pred_sents[:k]):
                    if p:
                        topIx = []
                        topIxAttn = []
                        for token, attn, state, cstar in zip(p,
                                                             trans.attns[ix],
                                                             batch_data[
                                                                 "target_states"][
                                                                 transIx][ix],
                                                             batch_data[
                                                                 'target_cstar'][
                                                                 transIx][ix]):
                            currentDec = {}
                            currentDec['token'] = token
                            currentDec['state'] = rr(list(state.data))
                            currentDec['cstar'] = rr(list(cstar.data))
                            topIx.append(currentDec)
                            topIxAttn.append(rr(list(attn)))
                            # if t in ['.', '!', '?']:
                            #     break
                        decoderRes.append(topIx)
                        attnRes.append(topIxAttn)
                res['scores'] = list(np.array(trans.pred_scores))[:k]
                res['decoder'] = decoderRes
                res['attn'] = attnRes
                # res['beam'] = batch_data['beam'][transIx]
                # todo: make nice...
                convert_to_py = lambda x: {"pred": x['pred'].item(),
                                           "score": x[
                                               'score'].item(),
                                           "state": rr(
                                               list(map(lambda s: s.item(),
                                                        x['state'])))
                                           }
                res['beam'] = list(map(lambda t:
                                       list(map(convert_to_py,
                                                t)),
                                       batch_data['beam'][transIx]))
                res['beam_trace'] = batch_data['beam_trace'][transIx]
                reply[transIx] = res
        return reply

    @staticmethod
    def textDataFromString(data, truncate, side):
        with io.StringIO(data) as corpus_file:
            for i, line in enumerate(corpus_file):
                line = line.strip().split()
                if truncate:
                    line = line[:truncate]

                words, feats, n_feats = \
                    TextDataset.extract_text_features(line)

                example_dict = {side: words, "indices": i}
                if feats:
                    prefix = side + "_feat_"
                    example_dict.update((prefix + str(j), f)
                                        for j, f in enumerate(feats))
                yield example_dict, n_feats

    @staticmethod
    def make_text_examples_nfeats_tpl(data, truncate, side):
        """
        Args:
            path (str): location of a src or tgt file.
            truncate (int): maximum sequence length (0 for unlimited).
            side (str): "src" or "tgt".

        Returns:
            (example_dict iterator, num_feats) tuple.


         src_examples_iter, num_src_feats = \
            TextDataset.make_text_examples_nfeats_tpl(
                src_path, src_seq_length_trunc, "src")
        """
        assert side in ['src', 'tgt']

        # All examples have same number of features, so we peek first one
        # to get the num_feats.
        examples_nfeats_iter = \
            ONMTmodelAPI.textDataFromString(data, truncate, side)

        first_ex = next(examples_nfeats_iter)
        num_feats = first_ex[1]

        # Chain back the first element - we only want to peek it.
        examples_nfeats_iter = chain([first_ex], examples_nfeats_iter)
        examples_iter = (ex for ex, nfeats in examples_nfeats_iter)

        return (examples_iter, num_feats)


def main():
    # model = ONMTmodelAPI("model/date_acc_100.00_ppl_1.00_e7.pt")
    model = ONMTmodelAPI(
        "../S2Splay/model_api/processing/s2s_iwslt_ende/baseline-brnn.en-de.s154_acc_61.58_ppl_7.43_e21.pt")
    # Simple Case
    # reply = model.translate(["This is a test ."], dump_data=False)
    # Case with attn overwrite OR partial
    reply = model.translate(["this is madness ."], attn_overwrite=[{2: 0}])
    # reply = model.translate(["this is madness ."], partial_decode=["das ist"])
    # Complex Case with attn and partial
    # reply = model.translate(["this is madness ."],
    #                         attn_overwrite=[{2:0}],
    #                         partial_decode=["das ist"])

    # Cases with multiple
    # reply = model.translate(["This is a test .", "and another one ."])
    # Partial
    # reply = model.translate(["This is a test .", "this is a second test ."],
    #                          partial_decode=["Dies ist", "Ein zweiter"])
    # Attn overwrite
    # reply = model.translate(["this is madness .", "i am awesome ."],
    #                         attn_overwrite=[{2:0}, {}])
    # All together - phew
    # reply = model.translate(["this is madness .", "i am awesome ."],
    #                         partial_decode=["heute ist", "du bist"],
    #                         attn_overwrite=[{2:0}, {2:2}])

    # Debug options
    # print("______")
    # print(len(reply[0]['decoder']))
    # print(len(reply[0]['decoder'][0]))
    # print(reply[0]['beam_trace'])
    # print(json.dumps(reply, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
