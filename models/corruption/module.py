import logging
import os
import random
import sys
random.seed(1230)
sys.path.append(os.path.join(os.getcwd()))

from augmenter import Augmenter
from sentence_transformers import SentenceTransformer
from textattack.transformations import WordInsertionMaskedLM, WordSwapMaskedLM, WordDeletion
# from textattack.constraints.semantics.sentence_encoders.sentence_encoder import SentenceEncoder

import tensorflow as tf
import transformers
from tqdm import tqdm

from utils.dataset_utils import get_result_txt
from utils.logging import getLogger



class Attacker:
    def __init__(self, attack_type, attack_percentage, path2txt, path2result, constraint_kwargs, augment_data_flag=False):
        log_dir = os.path.dirname(path2txt) if not augment_data_flag else "./data"
        self.logger = getLogger(f"CORRUPTION-{attack_type}",
                                        dir_=log_dir)
        self.logger.setLevel(logging.INFO)
        logFormatter = logging.Formatter("%(asctime)s - %(threadName)s - %(levelname)s - %(message)s",
                                         datefmt="%Y-%m-%d %H:%M:%S")
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        file_handler = logging.FileHandler(f"./{log_dir}/corruption.log", mode="w")
        file_handler.setFormatter(logFormatter)
        self.logger.addHandler(file_handler)

        self.logger.info(f"Initializing attacker with [{attack_type}] attack with rate=[{attack_percentage}]")
        self.logger.info(f"Parameters are {constraint_kwargs}")
        self.attack_type = attack_type
        self.num_sentence = constraint_kwargs.get('num_sentence', None)

        use_thres = constraint_kwargs.get("use", 0)
        constraints = [
            SentenceTransformerEncoder(
                threshold=use_thres,
                metric="cosine",
                compare_against_original=True,
                window_size=None)
        ]
        self.augmenter_choices = []
        transformations_per_example = 5 if augment_data_flag else 1

        if attack_type == "insertion" or augment_data_flag:
            shared_masked_lm = transformers.AutoModelForCausalLM.from_pretrained("distilroberta-base")
            shared_tokenizer = transformers.AutoTokenizer.from_pretrained("distilroberta-base")
            transformation = WordInsertionMaskedLM(
                masked_language_model=shared_masked_lm,
                tokenizer=shared_tokenizer,
                max_candidates=50,
                min_confidence=0.0,
            )
            self.augmenter_choices.append(Augmenter(transformation=transformation,
                                                    transformations_per_example=transformations_per_example,
                                                    constraints=constraints,
                                                    pct_words_to_swap=attack_percentage, fast_augment=True))

        elif attack_type == "substitution" or augment_data_flag:
            transformation = WordSwapMaskedLM(
                method="bae", #uses bert-base-uncased by default
                max_candidates=10,
                min_confidence=0.3,
                window_size=None
            )
            self.augmenter_choices.append(Augmenter(transformation=transformation,
                                                    transformations_per_example=transformations_per_example,
                                                    constraints=constraints,
                                                    pct_words_to_swap=attack_percentage, fast_augment=True))
        elif attack_type == "char-based" or augment_data_flag:
            pass

        elif attack_type == "deletion" or augment_data_flag:
            transformation = WordDeletion()
            self.augmenter_choices.append(Augmenter(transformation=transformation,
                                                    transformations_per_example=transformations_per_example,
                                                    constraints=constraints,
                                                    pct_words_to_swap=attack_percentage, fast_augment=True))

        self.path2txt = path2txt
        self.result_dir = path2result
        self.wr = open(path2result, "w")


    def attack_sentence(self, attack_all_samples=True):
        self.texts = get_result_txt(self.path2txt)
        skipped_cnt = 0
        attacked_cnt = 0
        for idx, (c_idx, sen_idx, sub_idset, sub_idx, wm_sen, key, msg) in enumerate(tqdm(self.texts)):
            self.logger.info(f"{c_idx} {sen_idx}")
            if attack_all_samples or len(msg) > 0:
                corrupted_sentence = self.augmenter_choices[0].augment(wm_sen)[0]
                self.logger.info(corrupted_sentence)
                self.logger.info(wm_sen)
                if corrupted_sentence == wm_sen:
                    skipped_cnt += 1
                attacked_cnt += 1

            else:
                corrupted_sentence = wm_sen
                skipped_cnt += 1

            self.wr.write(corrupted_sentence + "\n")
            if self.num_sentence and self.num_sentence == attacked_cnt:
                break

        self.wr.close()
        self.logger.info(f"Skipped {skipped_cnt} and created {attacked_cnt} sentences")

    def attack_sample(self):
        sentence_agg = []
        augmented_data = []
        prev_idx = 0

        for idx, (c_idx, sen_idx, sub_idset, sub_idx, wm_sen, key, msg) in enumerate(tqdm(self.texts)):
            if prev_idx == c_idx:
                sentence_agg.append(wm_sen)
                if idx != len(self.texts)-1: # for the last sample, do not continue to the next loop
                    continue

            while True:
                random_sen_idx = random.choice(range(len(sentence_agg)))
                clean_sentence = sentence_agg[random_sen_idx]
                corrupted_sentence = self.augmenter_choices[0].augment(clean_sentence)[0]
                if len(clean_sentence) != len(corrupted_sentence):
                    break

            sentence_agg[random_sen_idx] = corrupted_sentence
            for sen in sentence_agg:
                self.wr.write(sen + "\n")

            self.wr.close()
            augmented_data.extend(sentence_agg)
            sentence_agg = [wm_sen]
            prev_idx = c_idx

    def augment_data(self, cover_texts):
        attacked_cnt = 0
        for c_idx, sentences in enumerate(cover_texts):
            self.logger.info(f"{c_idx}")
            for sen in sentences:
                random_augment = random.choice(self.augmenter_choices)
                corrupted_sentences = random_augment.augment(sen.text)
                if corrupted_sentences:
                    data2write = corrupted_sentences
                    data2write.insert(0, sen.text)
                    self.wr.write("[sep] ".join(data2write) + "\n")
                    attacked_cnt += 1

                if attacked_cnt >= self.num_sentence:
                    self.logger.info(f"Done augmenting {attacked_cnt} sentences")
                    break
        self.wr.close()





"""
Sentence Encoder Class from TextAttack modified to set number of corruptions words = round(ratio * length) 
------------------------
"""

from abc import ABC
import math

import numpy as np
import torch

from textattack.constraints import Constraint


class SentenceEncoder(Constraint, ABC):
    """Constraint using cosine similarity between sentence encodings of x and
    x_adv.

    Args:
        threshold (:obj:`float`, optional): The threshold for the constraint to be met.
            Defaults to 0.8
        metric (:obj:`str`, optional): The similarity metric to use. Defaults to
            cosine. Options: ['cosine, 'angular']
        compare_against_original (bool):  If `True`, compare new `x_adv` against the original `x`.
            Otherwise, compare it against the previous `x_adv`.
        window_size (int): The number of words to use in the similarity
            comparison. `None` indicates no windowing (encoding is based on the
            full input).
    """

    def __init__(
        self,
        threshold=0.8,
        metric="cosine",
        compare_against_original=True,
        window_size=None,
        skip_text_shorter_than_window=False,
    ):
        super().__init__(compare_against_original)
        self.metric = metric
        self.threshold = threshold
        self.window_size = window_size
        self.skip_text_shorter_than_window = skip_text_shorter_than_window

        if not self.window_size:
            self.window_size = float("inf")

        if metric == "cosine":
            self.sim_metric = torch.nn.CosineSimilarity(dim=1)
        elif metric == "angular":
            self.sim_metric = get_angular_sim
        elif metric == "max_euclidean":
            # If the threshold requires embedding similarity measurement
            # be less than or equal to a certain value, just negate it,
            # so that we can still compare to the threshold using >=.
            self.threshold = -threshold
            self.sim_metric = get_neg_euclidean_dist
        else:
            raise ValueError(f"Unsupported metric {metric}.")

    def encode(self, sentences):
        """Encodes a list of sentences.

        To be implemented by subclasses.
        """
        raise NotImplementedError()

    def _sim_score(self, starting_text, transformed_text):
        """Returns the metric similarity between the embedding of the starting
        text and the transformed text.

        Args:
            starting_text: The ``AttackedText``to use as a starting point.
            transformed_text: A transformed ``AttackedText``

        Returns:
            The similarity between the starting and transformed text using the metric.
        """
        try:
            modified_index = next(
                iter(transformed_text.attack_attrs["newly_modified_indices"])
            )
        except KeyError:
            raise KeyError(
                "Cannot apply sentence encoder constraint without `newly_modified_indices`"
            )
        starting_text_window = starting_text.text_window_around_index(
            modified_index, self.window_size
        )

        transformed_text_window = transformed_text.text_window_around_index(
            modified_index, self.window_size
        )

        starting_embedding, transformed_embedding = self.model.encode(
            [starting_text_window, transformed_text_window]
        )

        if not isinstance(starting_embedding, torch.Tensor):
            starting_embedding = torch.tensor(starting_embedding)

        if not isinstance(transformed_embedding, torch.Tensor):
            transformed_embedding = torch.tensor(transformed_embedding)

        starting_embedding = torch.unsqueeze(starting_embedding, dim=0)
        transformed_embedding = torch.unsqueeze(transformed_embedding, dim=0)

        return self.sim_metric(starting_embedding, transformed_embedding)

    def _score_list(self, starting_text, transformed_texts):
        """Returns the metric similarity between the embedding of the starting
        text and a list of transformed texts.

        Args:
            starting_text: The ``AttackedText``to use as a starting point.
            transformed_texts: A list of transformed ``AttackedText``

        Returns:
            A list with the similarity between the ``starting_text`` and each of
                ``transformed_texts``. If ``transformed_texts`` is empty,
                an empty tensor is returned
        """
        # Return an empty tensor if transformed_texts is empty.
        # This prevents us from calling .repeat(x, 0), which throws an
        # error on machines with multiple GPUs (pytorch 1.2).
        if len(transformed_texts) == 0:
            return torch.tensor([])

        if self.window_size:
            starting_text_windows = []
            transformed_text_windows = []
            for transformed_text in transformed_texts:
                # @TODO make this work when multiple indices have been modified
                try:
                    modified_index = next(
                        iter(transformed_text.attack_attrs["newly_modified_indices"])
                    )
                except KeyError:
                    raise KeyError(
                        "Cannot apply sentence encoder constraint without `newly_modified_indices`"
                    )
                except StopIteration:
                    continue

                starting_text_windows.append(
                    starting_text.text_window_around_index(
                        modified_index, self.window_size
                    )
                )
                transformed_text_windows.append(
                    transformed_text.text_window_around_index(
                        modified_index, self.window_size
                    )
                )
            embeddings = self.encode(starting_text_windows + transformed_text_windows)
            if not isinstance(embeddings, torch.Tensor):
                embeddings = torch.tensor(embeddings)
            starting_embeddings = embeddings[: len(transformed_texts)]
            transformed_embeddings = embeddings[len(transformed_texts) :]
        else:
            starting_raw_text = starting_text.text
            transformed_raw_texts = [t.text for t in transformed_texts]
            embeddings = self.encode([starting_raw_text] + transformed_raw_texts)
            if not isinstance(embeddings, torch.Tensor):
                embeddings = torch.tensor(embeddings)

            starting_embedding = embeddings[0]

            transformed_embeddings = embeddings[1:]

            # Repeat original embedding to size of perturbed embedding.
            starting_embeddings = starting_embedding.unsqueeze(dim=0).repeat(
                len(transformed_embeddings), 1
            )

        return self.sim_metric(starting_embeddings, transformed_embeddings)

    def _check_constraint_many(self, transformed_texts, reference_text):
        """Filters the list ``transformed_texts`` so that the similarity
        between the ``reference_text`` and the transformed text is greater than
        the ``self.threshold``."""
        scores = self._score_list(reference_text, transformed_texts)

        for i, transformed_text in enumerate(transformed_texts):
            # Optionally ignore similarity score for sentences shorter than the
            # window size.
            if (
                self.skip_text_shorter_than_window
                and len(transformed_text.words) < self.window_size
            ):
                scores[i] = 1
            transformed_text.attack_attrs["similarity_score"] = scores[i].item()
        mask = (scores >= self.threshold).cpu().numpy().nonzero()
        return np.array(transformed_texts)[mask]

    def _check_constraint(self, transformed_text, reference_text):
        if (
            self.skip_text_shorter_than_window
            and len(transformed_text.words) < self.window_size
        ):
            score = 1
        else:
            score = self._sim_score(reference_text, transformed_text)

        transformed_text.attack_attrs["similarity_score"] = score
        return score >= self.threshold

    def extra_repr_keys(self):
        return [
            "metric",
            "threshold",
            "window_size",
            "skip_text_shorter_than_window",
        ] + super().extra_repr_keys()


def get_angular_sim(emb1, emb2):
    """Returns the _angular_ similarity between a batch of vector and a batch
    of vectors."""
    cos_sim = torch.nn.CosineSimilarity(dim=1)(emb1, emb2)
    return 1 - (torch.acos(cos_sim) / math.pi)


def get_neg_euclidean_dist(emb1, emb2):
    """Returns the Euclidean distance between a batch of vectors and a batch of
    vectors."""
    return -torch.sum((emb1 - emb2) ** 2, dim=1)


class SentenceTransformerEncoder(SentenceEncoder):
    def __init__(self, **kwargs):
        super(SentenceTransformerEncoder, self).__init__(**kwargs)
        self.embedder = SentenceTransformer('all-MiniLM-L6-v2')
        self.window_size = kwargs['window_size']


    def encode(self, sentences):
        return self.embedder.encode(sentences, convert_to_tensor=True)
