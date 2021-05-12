# Some DaveNet scoring code is borrowed from from https://github.com/iapalm/davenet_demo/tree/main/models

import math
import sys
import time
import os.path
import numpy as np
import scipy.signal
import scipy.misc
import librosa
import torch
import torch.nn as nn
import torchvision.transforms as transforms
import dave_models
import matplotlib.pyplot as plt
import tarfile
import torchvision
import pandas as pd

from PIL import Image
from googlenet_places205 import GoogLeNetPlaces205
from googlenet_places205_caffe import GoogleNetPlaces205Caffe
from dataloaders.image_caption_dataset import ImageCaptionDataset

CLASSES_205_PATH = "eval_scorers/trained_models/googlenet_places205/categoryindex_places205.csv"
NIKHIL_CAFFE_GOOGLENET_PLACES205_PATH = "eval_scorers/trained_models/googlenet_places205/snapshot_iter_765280.caffemodel.pt"
CAFFE_GOOGLENET_PLACES205_PATH = "eval_scorers/trained_models/googlenet_places205/2755ce3d87254759a25cd82e3ca86c4a.npy"
GOOGLENET_PLACES205_PATH = 'eval_scorers/trained_models/googlenet_places205/googlenet_places205.pth' 
DAVENET_MODEL_PATH = 'eval_scorers/trained_models/davenet_vgg16_MISA_1024_pretrained/'
AUDIO_MODEL_PATH = os.path.join(DAVENET_MODEL_PATH, 'audio_model.pth')
IMAGE_MODEL_PATH = os.path.join(DAVENET_MODEL_PATH, 'image_model.pth')

TEST_DATA_DIR = "./data/"
DATASET_BASE_PATH = os.path.join(TEST_DATA_DIR, "PlacesAudioEnglish")
TAR_FILE_PATH = DATASET_BASE_PATH + ".tar.gz"


class DaveNetScorer():
    def __init__(self, audio_model_path, image_model_path, matchmap_thresh = 5.0):
        self.audio_model, self.image_model = dave_models.DAVEnet_model_loader(audio_model_path, image_model_path)
        self.audio_model.eval()
        self.image_model.eval()
        self.image_transform = transforms.Compose(
                [
                transforms.ToPILImage(mode='RGB'),
                transforms.Resize(256),
                transforms.ToTensor(),
                transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
                ])

        self.matchmap_thresh = matchmap_thresh


    def get_image_features(self, img):
        with torch.no_grad():
            self.image_model.eval()
            image_transformed = self.image_transform(img).unsqueeze(0)
            image_feature_map = self.image_model(image_transformed).squeeze(0)
            emb_dim = image_feature_map.size(0)
            output_H = image_feature_map.size(1)
            output_W = image_feature_map.size(2)
            return image_feature_map.view(emb_dim, output_H * output_W), (emb_dim, output_H, output_W)


    def get_audio_features(self, melspec):
        with torch.no_grad():
            audio_output = self.audio_model(melspec.unsqueeze(0).unsqueeze(0)).squeeze(0)
            return audio_output


    def score(self, melspec, img):
        # Scores produced using metrics defined by https://arxiv.org/pdf/1804.01452.pdf
        image_output, image_dim = self.get_image_features(img)
        audio_output = self.get_audio_features(melspec)
        _, img_output_H, img_output_W = image_dim
        heatmap = torch.mm(audio_output.t(), image_output).squeeze()
        heatmap = heatmap.view(audio_output.size(1), img_output_H, img_output_W).numpy()#.max(dim=0)[0].numpy()

        matches = np.where(heatmap >= self.matchmap_thresh, 0, 1)
        N_t = audio_output.size(1)
        N_r = img_output_H
        N_c = img_output_W
        sisa = np.sum(heatmap) / (N_t * N_r * N_c)
        misa = np.sum(np.max(heatmap.reshape(N_t, N_r * N_c), axis = 1)) / (N_t)
        sima = np.sum(np.max(heatmap, axis = 0)) / (N_r * N_c)
        return heatmap, matches, sisa, misa, sima   


class ClassifierScorer():
    def __init__(self, model_type = "GoogleNetPlaces205CaffeNikhil"):
        self.model = None

        if model_type == "GoogLeNetPlaces205":
            self.model = GoogLeNetPlaces205()
            self.model.load_state_dict(torch.load(CAFFE_GOOGLENET_PLACES205_PATH))

        elif model_type == "GoogleNetPlaces205Caffe":
            self.model = GoogleNetPlaces205Caffe(CAFFE_GOOGLENET_PLACES205_PATH)

        elif model_type == "GoogleNetPlaces205CaffeNikhil":
            layer_map = {"conv1_1": "features.0",
                         "conv1_2": "features.2", 
                         "conv2_1": "features.5", 
                         "conv2_2": "features.7",
                         "conv3_1": "features.10",
                         "conv3_2": "features.12", 
                         "conv3_3": "features.14",
                         "conv4_1": "features.17", 
                         "conv4_2": "features.19", 
                         "conv4_3": "features.21",
                         "conv5_1": "features.24", 
                         "conv5_2": "features.26", 
                         "conv5_3": "features.28", 
                         "fc6": "classifier.0",
                         "fc7": "classifier.3", 
                         "fc8": "classifier.6"}
            self.model = torchvision.models.vgg16(num_classes=205)
            s = torch.load(NIKHIL_CAFFE_GOOGLENET_PLACES205_PATH)
            self.model.load_state_dict({self.replace(kn, layer_map):v for kn, v in s.items()})   

        self.model.eval()

    def replace(self, key, mapping):
        k = key[:key.rfind(".")]
        return key.replace(k, mapping[k])

    def score(self, img):
        if self.model is None:
            return None

        return self.model(img) 


def main():
    dave_scorer = DaveNetScorer(AUDIO_MODEL_PATH, IMAGE_MODEL_PATH)
    clf_scorer = ClassifierScorer()

    if os.path.isfile(TAR_FILE_PATH):
          tar = tarfile.open(TAR_FILE_PATH, "r:gz")
          tar.extractall(path=TEST_DATA_DIR)

    audio_conf = {
        'use_raw_length': True
    }
    loader = ImageCaptionDataset(os.path.join(DATASET_BASE_PATH, "samples.json"),
        audio_conf = audio_conf)

    lnet_img_transform = transforms.Compose([
            transforms.Resize(224)
        ])

    class_205 = pd.read_csv(CLASSES_205_PATH, header = None)

    def get_class(uid):
        return class_205.iloc[uid][0].split(' ')[0].split('/')[-1]


    prev_audio = None
    for img, audio, n_frames in loader:
        heatmap, matches, sisa, misa, sima  = dave_scorer.score(audio, img)
        lnet_img = lnet_img_transform(img)
        clf_score = clf_scorer.score(lnet_img.unsqueeze(0))
        print("Pred Class: ", get_class(torch.argmax(clf_score).numpy()))
        prev_audio = audio
        fig, ax = plt.subplots(1, 2, figsize=(25, 5), gridspec_kw={'width_ratios': [1, 3]})
        ax[0].imshow(img.permute(1, 2, 0))
        ax[1].imshow(audio, aspect='auto')
        plt.show()


if __name__ == "__main__":
    main()
