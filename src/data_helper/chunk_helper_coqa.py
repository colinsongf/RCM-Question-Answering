"""
chunk_helper.py
 - utility functions to chunk documents with a fixed stride size
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import collections
import logging
import json
import math
import os
import random
import pickle
from tqdm import tqdm, trange

import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from transformers.tokenization_bert import whitespace_tokenize, BasicTokenizer, BertTokenizer

from data_helper.qa_util import _improve_answer_span, _get_best_indexes, get_final_text, \
     _check_is_max_context, _compute_softmax
from data_helper.data_helper_coqa import CoQAExample

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)



class ChunkFeature(object):
    def __init__(self,
                 unique_id,
                 example_index,
                 doc_span_index,
                 tokens,
                 token_to_orig_map,
                 token_is_max_context,
                 input_ids,
                 input_mask,
                 segment_ids,
                 start_position=None,
                 end_position=None,
                 yes_no_flag=None,
                 yes_no_ans=None):
        self.unique_id = unique_id
        self.example_index = example_index
        self.doc_span_index = doc_span_index
        self.tokens = tokens
        self.token_to_orig_map = token_to_orig_map
        self.token_is_max_context = token_is_max_context
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.start_position = start_position
        self.end_position = end_position
        self.yes_no_flag = yes_no_flag
        self.yes_no_ans = yes_no_ans
        

def convert_examples_to_features(examples, tokenizer, max_seq_length,
                                 doc_stride, max_query_length, is_training, append_history):
    """
    Features for each chunk from a document
    A document can have multiple equally-spaced chunks
    """
    unique_id = 1000000000
    features = []
    for (example_index, example) in enumerate(examples):
        # query_tokens
        all_query_tokens = [tokenizer.tokenize(question_text) for question_text in example.question_texts]
        if append_history:
            all_query_tokens = all_query_tokens[::-1]
        flat_all_query_tokens = []
        for query_tokens in all_query_tokens:
            flat_all_query_tokens += query_tokens
        if append_history:
            query_tokens = flat_all_query_tokens[:max_query_length]
        else:
            query_tokens = flat_all_query_tokens[-1*max_query_length:]
            
        tok_to_orig_index = []
        orig_to_tok_index = []
        all_doc_tokens = []
        for (i, token) in enumerate(example.doc_tokens):
            orig_to_tok_index.append(len(all_doc_tokens))
            sub_tokens = tokenizer.tokenize(token)
            for sub_token in sub_tokens:
                tok_to_orig_index.append(i)
                all_doc_tokens.append(sub_token)

        tok_start_position = None
        tok_end_position = None
        if is_training:
            tok_start_position = orig_to_tok_index[example.start_position]
            if example.end_position < len(example.doc_tokens) - 1:
                tok_end_position = orig_to_tok_index[example.end_position + 1] - 1
            else:
                tok_end_position = len(all_doc_tokens) - 1
            (tok_start_position, tok_end_position) = _improve_answer_span(
                all_doc_tokens, tok_start_position, tok_end_position, tokenizer,
                example.orig_answer_text)
        # The -3 accounts for [CLS], [SEP] and [SEP]
        max_tokens_for_doc = max_seq_length - len(query_tokens) - 3
        # sliding window to generate multiple document spans
        _DocSpan = collections.namedtuple(
            "DocSpan", ["start", "length"])
        doc_spans = []
        start_offset = 0
        while start_offset < len(all_doc_tokens):
            length = len(all_doc_tokens) - start_offset
            if length > max_tokens_for_doc:
                length = max_tokens_for_doc
            doc_spans.append(_DocSpan(start=start_offset, length=length))
            if start_offset + length == len(all_doc_tokens):
                break
            start_offset += min(length, doc_stride)

        for (doc_span_index, doc_span) in enumerate(doc_spans):
            tokens = []
            token_to_orig_map = {}
            token_is_max_context = {}
            segment_ids = []
            tokens.append("[CLS]")
            segment_ids.append(0)
            for token in query_tokens:
                tokens.append(token)
                segment_ids.append(0)
            tokens.append("[SEP]")
            segment_ids.append(0)

            for i in range(doc_span.length):
                split_token_index = doc_span.start + i
                token_to_orig_map[len(tokens)] = tok_to_orig_index[split_token_index]

                is_max_context = _check_is_max_context(doc_spans, doc_span_index,
                                                       split_token_index)
                token_is_max_context[len(tokens)] = is_max_context
                tokens.append(all_doc_tokens[split_token_index])
                segment_ids.append(1)
            tokens.append("[SEP]")
            segment_ids.append(1)
            
            input_ids = tokenizer.convert_tokens_to_ids(tokens)
            # The mask has 1 for real tokens and 0 for padding tokens. Only real
            # tokens are attended to.
            input_mask = [1] * len(input_ids)
            # Zero-pad up to the sequence length.
            while len(input_ids) < max_seq_length:
                input_ids.append(0)
                input_mask.append(0)
                segment_ids.append(0)
            assert len(input_ids) == max_seq_length
            assert len(input_mask) == max_seq_length
            assert len(segment_ids) == max_seq_length

            start_position = None
            end_position = None
            yes_no_flag = None
            yes_no_ans = None
            if is_training:
                doc_start = doc_span.start
                doc_end = doc_span.start + doc_span.length - 1
                if (example.start_position < doc_start or
                        example.end_position < doc_start or
                        example.start_position > doc_end or example.end_position > doc_end):
                    continue
                doc_offset = len(query_tokens) + 2
                start_position = tok_start_position - doc_start + doc_offset
                end_position = tok_end_position - doc_start + doc_offset
                yes_no_flag = example.yes_no_flag
                yes_no_ans = example.yes_no_ans
            if example_index >= 16 and example_index < 20:
                logger.info("*** Example ***")
                logger.info("unique_id: %s" % (unique_id))
                logger.info("example_index: %s" % (example_index))
                logger.info("doc_span_index: %s" % (doc_span_index))
                logger.info("tokens: %s" % " ".join(tokens))
                if is_training:
                    answer_text = " ".join(tokens[start_position:(end_position + 1)])
                    logger.info("start_position: %d" % (start_position))
                    logger.info("end_position: %d" % (end_position))
                    logger.info("answer: %s" % (answer_text))
            features.append(
                ChunkFeature(
                    unique_id=unique_id,
                    example_index=example_index,
                    doc_span_index=doc_span_index,
                    tokens=tokens,
                    token_to_orig_map=token_to_orig_map,
                    token_is_max_context=token_is_max_context,
                    input_ids=input_ids,
                    input_mask=input_mask,
                    segment_ids=segment_ids,
                    start_position=start_position,
                    end_position=end_position,
                    yes_no_flag=yes_no_flag,
                    yes_no_ans=yes_no_ans))
            unique_id += 1
    return features


RawResult = collections.namedtuple("RawResult",
                                   ["unique_id", "start_logits", "end_logits", "yes_no_flag_logits", "yes_no_ans_logits"])


def make_predictions(all_examples, all_features, all_results, n_best_size,
                      max_answer_length, do_lower_case, verbose_logging,
                      validate_flag=True):
    example_index_to_features = collections.defaultdict(list)
    for feature in all_features:
        example_index_to_features[feature.example_index].append(feature)

    unique_id_to_result = {}
    for result in all_results:
        unique_id_to_result[result.unique_id] = result

    _PrelimPrediction = collections.namedtuple(
        "PrelimPrediction",
        ["feature_index", "start_index", "end_index", "text", "logit"])

    validate_predictions = dict()
    all_predictions = []
    all_nbest_json = []
    for (example_index, example) in enumerate(all_examples):
        features = example_index_to_features[example_index]

        prelim_predictions = []
        for (feature_index, feature) in enumerate(features):
            result = unique_id_to_result[feature.unique_id]
            # if yes-no question: prelim_predictions include either yes or no + spans
            # if wh- question: prelim_predictions only fnclde doc spans
            yes_no_flag_logits = result.yes_no_flag_logits # (2,)
            yes_no_ans_logits = result.yes_no_ans_logits # (2,)
            # yes-no question
            if (np.argmax(yes_no_flag_logits) == 1):
                if (np.argmax(yes_no_ans_logits) == 1):
                    text = 'yes'
                    logit = yes_no_flag_logits[1] + yes_no_ans_logits[1]
                else:
                    text = 'no'
                    logit = yes_no_flag_logits[1] + yes_no_ans_logits[0]
                prelim_predictions.append(
                    _PrelimPrediction(
                        feature_index=feature_index,
                        start_index=-1,
                        end_index=-1,
                        text=text,
                        logit=logit))
                continue
                    
            start_indexes = _get_best_indexes(result.start_logits, n_best_size)
            end_indexes = _get_best_indexes(result.end_logits, n_best_size)
            for start_index in start_indexes:
                for end_index in end_indexes:
                    # We could hypothetically create invalid predictions, e.g., predict
                    # that the start of the span is in the question. We throw out all
                    # invalid predictions.
                    if start_index >= len(feature.tokens):
                        continue
                    if end_index >= len(feature.tokens):
                        continue
                    if start_index not in feature.token_to_orig_map:
                        continue
                    if end_index not in feature.token_to_orig_map:
                        continue
                    if not feature.token_is_max_context.get(start_index, False):
                        continue
                    if end_index < start_index:
                        continue
                    length = end_index - start_index + 1
                    if length > max_answer_length:
                        continue
                    prelim_predictions.append(
                        _PrelimPrediction(
                            feature_index=feature_index,
                            start_index=start_index,
                            end_index=end_index,
                            text=None,
                            #logit=result.start_logits[start_index]+result.end_logits[end_index]))
                            logit=yes_no_flag_logits[0]+result.start_logits[start_index]+result.end_logits[end_index]))

        prelim_predictions = sorted(
            prelim_predictions,
            key=lambda x: x.logit,
            reverse=True)

        _NbestPrediction = collections.namedtuple(
            "NbestPrediction", ["text", "logit"])

        #_NbestPrediction = collections.namedtuple(  # pylint: disable=invalid-name
        #    "NbestPrediction", ["text", "start_logit", "end_logit"])

        seen_predictions = {}
        nbest = []
        for pred in prelim_predictions:
            if len(nbest) >= n_best_size:
                break
            feature = features[pred.feature_index]

            if (pred.start_index == -1 or pred.end_index == -1):
                final_text = pred.text
            else:
                tok_tokens = feature.tokens[pred.start_index:(pred.end_index + 1)]
                orig_doc_start = feature.token_to_orig_map[pred.start_index]
                orig_doc_end = feature.token_to_orig_map[pred.end_index]
                orig_tokens = example.doc_tokens[orig_doc_start:(orig_doc_end + 1)]
                tok_text = " ".join(tok_tokens)

                # De-tokenize WordPieces that have been split off.
                tok_text = tok_text.replace(" ##", "")
                tok_text = tok_text.replace("##", "")

                # Clean whitespace
                tok_text = tok_text.strip()
                tok_text = " ".join(tok_text.split())
                orig_text = " ".join(orig_tokens)

                final_text = get_final_text(tok_text, orig_text, do_lower_case, verbose_logging)
            
            
            if final_text in seen_predictions:
                continue
            seen_predictions[final_text] = True
            nbest.append(
                _NbestPrediction(
                    text=final_text,
                    logit=pred.logit))

            # for validation, only the best one prediction is needed
            if validate_flag:
                break

        # In very rare edge cases we could have no valid predictions. So we
        # just create a nonce prediction in this case to avoid failure.
        if not nbest:
            nbest.append(
                _NbestPrediction(text="empty", logit=0.0))

        assert len(nbest) >= 1

        if validate_flag:
            validate_predictions[(example.paragraph_id, example.turn_id)] = nbest[0].text
        else:
            total_scores = []
            for entry in nbest:
                #total_scores.append(entry.start_logit + entry.end_logit)
                total_scores.append(entry.logit)

            probs = _compute_softmax(total_scores)

            nbest_json = []
            for (i, entry) in enumerate(nbest):
                output = collections.OrderedDict()
                output["text"] = entry.text
                output["probability"] = probs[i]
                output["logit"] = entry.logit
                #output["start_logit"] = entry.start_logit
                #output["end_logit"] = entry.end_logit
                nbest_json.append(output)

            assert len(nbest_json) >= 1

            #all_predictions[(example.paragraph_id, example.turn_id)] = nbest_json[0]["text"]
            #all_nbest_json[(example.paragraph_id, example.turn_id)] = nbest_json
            cur_prediction = collections.OrderedDict()
            cur_prediction["id"] = example.paragraph_id
            cur_prediction["turn_id"] = example.turn_id
            cur_prediction["answer"] = nbest_json[0]["text"]
            all_predictions.append(cur_prediction)

            cur_nbest_json = collections.OrderedDict()
            cur_nbest_json["id"] = example.paragraph_id
            cur_nbest_json["turn_id"] = example.turn_id
            cur_nbest_json["answers"] = nbest_json
            all_nbest_json.append(cur_nbest_json)
    
    if validate_flag:
        return validate_predictions
    else:
        return all_predictions, all_nbest_json
