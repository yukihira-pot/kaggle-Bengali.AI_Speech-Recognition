import torch
import torchaudio
import torchaudio.transforms as tat
from datasets import load_metric
import os

from pathlib import Path
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

import pandas as pd
import pyctcdecode
import numpy as np

import pyctcdecode
import torch
from transformers import Wav2Vec2Processor, Wav2Vec2ForCTC
from transformers import TrainingArguments, Trainer, EarlyStoppingCallback
import warnings

warnings.filterwarnings("ignore")
torchaudio.set_audio_backend("soundfile")


# hyper-parameters
SR = 16000
torch.backends.cudnn.benchmark = True

ROOT = Path.cwd().parent
INPUT = ROOT / "input"
DATA = INPUT / "bengaliai-speech"
TRAIN = DATA / "train_mp3s"
TEST = DATA / "test_mp3s"

output_dir = INPUT / "saved_model_large-mms-1b-bengali"
MODEL_PATH = INPUT / "wav2vec2-large-mms-1b-bengali/"
LM_PATH = INPUT / "arijitx-full-model/wav2vec2-xls-r-300m-bengali/language_model"

SENTENCES_PATH = INPUT / "macro-normalization/normalized.csv"
INDEXES_PATH = INPUT / "dataset-overlaps-with-commonvoice-11-bn/indexes.csv"


processor = Wav2Vec2Processor.from_pretrained(MODEL_PATH)
vocab_dict = processor.tokenizer.get_vocab()
vocab_dict = vocab_dict["ben"]
vocab_dict["<s>"] = 64
vocab_dict["</s>"] = 65
sorted_vocab_dict = {
    k: v for k, v in sorted(vocab_dict.items(), key=lambda item: item[1])
}

decoder = pyctcdecode.build_ctcdecoder(
    list(sorted_vocab_dict.keys()),
    str(LM_PATH) + "/5gram.bin",
)

# - From @mbmmurad's [Dataset overlaps with CommonVoice 11 bn](https://www.kaggle.com/code/mbmmurad/dataset-overlaps-with-commonvoice-11-bn), The competition dataset might contain the audios of the mozilla-foundation/common_voice_11_0 dataset. Here I just simply exclude them from the validation set.
# - Also, I use @UmongSain's normalized data [here](https://www.kaggle.com/code/umongsain/macro-normalization/notebook). Thanks to him!


indexes = set(pd.read_csv(INDEXES_PATH)["id"])
sentences = pd.read_csv(
    DATA / "train_normalized_with_noise_info.csv",
    dtype={
        "id": str,
        "mos_pred": float,
        "noi_pred": float,
        "dis_pred": float,
        "col_pred": float,
        "loud_pred": float,
        "model": str,
    },
)

print("sentence length", len(sentences))

# sentence の中で、mos_pred が NaN または mos_pred が 1.5 以下のものを除外
sentences = sentences[
    ~((sentences["mos_pred"].isnull()) | (sentences["mos_pred"] <= 1.5))
]

print("sentence length", len(sentences))
sentences = sentences[
    ~((sentences.index.isin(indexes)) & (sentences["split"] == "train"))
].reset_index(drop=True)


print("sentence length", len(sentences))


# * sample 10% data from "valid" part into validation set, 90% into training set.
# * sample 50% data from "train" part, and additionally sample 8% from it into validation set, 92% into training set.


data_0 = sentences.loc[sentences["split"] == "valid"].reset_index(drop=True)
valid_0 = data_0.sample(frac=0.1, random_state=42)
train_0 = data_0[~data_0.index.isin(valid_0.index)]

data_1 = (
    sentences.loc[sentences["split"] == "train"]
    .reset_index(drop=True)
    .sample(frac=0.50, random_state=42)
)
valid_1 = data_1.sample(frac=0.08, random_state=42)
train_1 = data_1[~data_1.index.isin(valid_1.index)]

train = (
    pd.concat([train_0, train_1], axis=0)
    .sample(frac=1, random_state=42)
    .reset_index(drop=True)
)
valid = (
    pd.concat([valid_0, valid_1], axis=0)
    .sample(frac=1, random_state=42)
    .reset_index(drop=True)
)

del data_0, data_1, valid_0, valid_1, train_0, train_1

all_ids = sentences["id"].to_list()
train_ids = train["id"].to_list()
valid_ids = valid["id"].to_list()

valid = valid.sample(n=3000, random_state=42)

thresh_size = 50000

train_ids = [
    train_id
    for train_id in train_ids
    if os.path.getsize(str(TRAIN / train_id) + ".mp3") < thresh_size
]
valid_ids = [
    valid_id
    for valid_id in valid_ids
    if os.path.getsize(str(TRAIN / valid_id) + ".mp3") < thresh_size
]

train_ids = sorted(
    train_ids, key=lambda train_id: os.path.getsize(str(TRAIN / train_id) + ".mp3")
)

# train_ids[0], train_ids[n], train_ids[1], train_ids[n-1], train_ids[2], train_ids[n-2], ... となるようにする
train_ids_aligned = []
for i in range(len(train_ids) // 2):
    train_ids_aligned.append(train_ids[i])
    train_ids_aligned.append(train_ids[len(train_ids) - 1 - i])

train_ids = train_ids_aligned

# valid_ids についても同様に
valid_ids = sorted(
    valid_ids, key=lambda valid_id: os.path.getsize(str(TRAIN / valid_id) + ".mp3")
)

valid_ids_aligned = []
for i in range(len(valid_ids) // 2):
    valid_ids_aligned.append(valid_ids[i])
    valid_ids_aligned.append(valid_ids[len(valid_ids) - 1 - i])

valid_ids = valid_ids_aligned


class W2v2Dataset(torch.utils.data.Dataset):
    def __init__(self, df):
        self.df = df
        self.pathes = df["id"].values
        self.sentences = df["sentence_normalized"].values
        self.resampler = tat.Resample(32000, SR)

    def __getitem__(self, idx):
        apath = TRAIN / f"{self.pathes[idx]}.mp3"
        waveform, sample_rate = torchaudio.load(apath, format="mp3")
        waveform = self.resampler(waveform)
        batch = dict()
        y = processor(waveform.reshape(-1), sampling_rate=SR).input_values[0]
        batch["input_values"] = y
        with processor.as_target_processor():
            batch["labels"] = processor(self.sentences[idx]).input_ids

        return batch

    def __len__(self):
        return len(self.df)


train_dataset = W2v2Dataset(train)
valid_dataset = W2v2Dataset(valid)


@dataclass
class DataCollatorCTCWithPadding:
    """
    Data collator that will dynamically pad the inputs received.
    Args:
        processor (:class:`~transformers.Wav2Vec2Processor`)
            The processor used for proccessing the data.
        padding (:obj:`bool`, :obj:`str` or :class:`~transformers.tokenization_utils_base.PaddingStrategy`, `optional`, defaults to :obj:`True`):
            Select a strategy to pad the returned sequences (according to the model's padding side and padding index)
            among:
            * :obj:`True` or :obj:`'longest'`: Pad to the longest sequence in the batch (or no padding if only a single
              sequence if provided).
            * :obj:`'max_length'`: Pad to a maximum length specified with the argument :obj:`max_length` or to the
              maximum acceptable input length for the model if that argument is not provided.
            * :obj:`False` or :obj:`'do_not_pad'` (default): No padding (i.e., can output a batch with sequences of
              different lengths).
        max_length (:obj:`int`, `optional`):
            Maximum length of the ``input_values`` of the returned list and optionally padding length (see above).
        max_length_labels (:obj:`int`, `optional`):
            Maximum length of the ``labels`` returned list and optionally padding length (see above).
        pad_to_multiple_of (:obj:`int`, `optional`):
            If set will pad the sequence to a multiple of the provided value.
            This is especially useful to enable the use of Tensor Cores on NVIDIA hardware with compute capability >=
            7.5 (Volta).
    """

    processor: Wav2Vec2Processor
    padding: Union[bool, str] = True
    max_length: Optional[int] = None
    max_length_labels: Optional[int] = None
    pad_to_multiple_of: Optional[int] = None
    pad_to_multiple_of_labels: Optional[int] = None

    def __call__(
        self, features: List[Dict[str, Union[List[int], torch.Tensor]]]
    ) -> Dict[str, torch.Tensor]:
        # split inputs and labels since they have to be of different lenghts and need
        # different padding methods
        input_features = [
            {"input_values": feature["input_values"]} for feature in features
        ]
        label_features = [{"input_ids": feature["labels"]} for feature in features]

        batch = self.processor.pad(
            input_features,
            padding=self.padding,
            max_length=self.max_length,
            pad_to_multiple_of=self.pad_to_multiple_of,
            return_tensors="pt",
        )
        with self.processor.as_target_processor():
            labels_batch = self.processor.pad(
                label_features,
                padding=self.padding,
                max_length=self.max_length_labels,
                pad_to_multiple_of=self.pad_to_multiple_of_labels,
                return_tensors="pt",
            )

        # replace padding with -100 to ignore loss correctly
        labels = labels_batch["input_ids"].masked_fill(
            labels_batch.attention_mask.ne(1), -100
        )

        batch["labels"] = labels

        return batch


data_collator = DataCollatorCTCWithPadding(processor=processor, padding=True)




wer_metric = load_metric("wer")

def compute_metrics(pred):
    pred_logits = pred.predictions
    pred_ids = np.argmax(pred_logits, axis=-1)

    pred.label_ids[pred.label_ids == -100] = processor.tokenizer.pad_token_id

    pred_str = processor.batch_decode(pred_ids)
    # we do not want to group tokens when computing the metrics
    label_str = processor.batch_decode(pred.label_ids, group_tokens=False)

    wer = wer_metric.compute(predictions=pred_str, references=label_str)

    return {"wer": wer}


model = Wav2Vec2ForCTC.from_pretrained(
    MODEL_PATH,
    attention_dropout=0.1,
    hidden_dropout=0.1,
    feat_proj_dropout=0.0,
    mask_time_prob=0.05,
    layerdrop=0.1,
    # gradient_checkpointing=True,
    ctc_loss_reduction="mean",
    pad_token_id=processor.tokenizer.pad_token_id,
    vocab_size=len(processor.tokenizer),
    ctc_zero_infinity=True,
    diversity_loss_weight=100,
)


# you can freeze some params
model.freeze_feature_extractor()

training_args = TrainingArguments(
    output_dir=output_dir,
    overwrite_output_dir=True,
    group_by_length=False,
    lr_scheduler_type="cosine",
    weight_decay=0.01,
    per_device_train_batch_size=4,
    per_device_eval_batch_size=16,
    gradient_accumulation_steps=1,
    evaluation_strategy="steps",
    save_strategy="steps",
    # max_steps=80000,  # you can change to "num_train_epochs"
    num_train_epochs=5,
    fp16=True,
    save_steps=4000,
    eval_steps=2000,
    logging_steps=1000,
    learning_rate=5e-5,
    warmup_steps=600,
    save_total_limit=1,
    load_best_model_at_end=True,
    # metric_for_best_model="wer",
    # greater_is_better=False,
    prediction_loss_only=False,
    auto_find_batch_size=True,
    report_to="none",
)


trainer = Trainer(
    model=model,
    data_collator=data_collator,
    args=training_args,
    # compute_metrics=compute_metrics,
    train_dataset=train_dataset,
    eval_dataset=valid_dataset,
    tokenizer=processor.feature_extractor,
    callbacks=[EarlyStoppingCallback(early_stopping_patience=5)],
)


trainer.train()


trainer.save_model(output_dir)

model.save_pretrained(output_dir)
processor.feature_extractor.save_pretrained(output_dir)
