import unittest
from gpmt.data import GeneratorData
from gpmt.model import Encoder, PositionalEncoding, StackDecoderLayer
from gpmt.utils import init_hidden, init_stack, get_default_tokens

gen_data_path = '../data/chembl_xsmall.smi'
tokens = get_default_tokens()
print(f'Number of tokens = {len(tokens)}')
gen_data = GeneratorData(training_data_path=gen_data_path, delimiter='\t',
                         cols_to_read=[0], keep_header=True, tokens=None, token_reload=True)

bz = 32


class MyTestCase(unittest.TestCase):

    def test_batch(self):
        batch = gen_data.random_training_set(batch_size=bz)
        assert (len(batch[0]) == bz and len(batch[1]) == bz)

    def test_embeddings(self):
        x, y = gen_data.random_training_set(batch_size=bz)
        encoder = Encoder(gen_data.n_characters, 128, tokens.index(' '))
        x = encoder(x)
        assert (x.ndim == 3)
        print(f'x.shape = {x.shape}')

    def test_positional_encodings(self):
        x, y = gen_data.random_training_set(batch_size=bz)
        encoder = Encoder(gen_data.n_characters, 128, tokens.index(' '))
        x = encoder(x)
        enc_shape = x.shape
        pe = PositionalEncoding(128, dropout=.2, max_len=500)
        x = pe(x)
        assert (x.shape == enc_shape)
        print(f'x.shape = {x.shape}')

    def test_stack_decoder_layer(self):
        x, y = gen_data.random_training_set(batch_size=bz)
        d_model = 128
        d_hidden = 10
        s_width = 16
        s_depth = 20
        encoder = Encoder(gen_data.n_characters, d_model, tokens.index(' '))
        x = encoder(x)
        pe = PositionalEncoding(d_model, dropout=.2, max_len=500)
        x = pe(x)
        h0 = init_hidden(x.shape[1], x.shape[0], d_hidden)
        s0 = init_stack(x.shape[1], x.shape[0], s_depth, s_width)
        stack_decoder = StackDecoderLayer(d_model=d_model, num_heads=1,
                                          d_hidden=d_hidden, stack_depth=s_depth,
                                          stack_width=s_width, dropout=.1)
        out = stack_decoder((x, h0, s0))
        assert (len(out) == 3)


if __name__ == '__main__':
    unittest.main()
