import os
import random
import numpy as np
from pathlib import Path
import polars as pl

from ebrec.utils._polars import slice_join_dataframes, concat_str_columns
from ebrec.utils._behaviors import (
    create_binary_labels_column,
    sampling_strategy_wu2019,
    truncate_history,
)
from ebrec.utils._articles_behaviors import map_list_article_id_to_value
from ebrec.utils._python import (
    repeat_by_list_values_from_matrix,
    create_lookup_objects,
)
from ebrec.utils._articles import create_article_id_to_value_mapping, convert_text2encoding_with_transformers

from torch.utils.data import Dataset

class EB_NeRDDataset(Dataset):
    def __init__(self, tokenizer, neg_sampling=True, split='train',**kwargs):
        '''
            kwargs: data_dir, history_size, batch_size
            
            return (his_input_title, pred_input_title), y which is
            the tokenized history and the tokenized articles in the inview list together with the labels which is the click or not click based on the inview list
        '''
        #TODO: JE: Also make sure the dataloader works with negative sampling is False (doesn't work now because labels different sizes, see eval for better)
        self.tokenizer = tokenizer
        self.split = split
        self.neg_sampling = neg_sampling
        self.eval_mode = False if split == 'train' else True
        # Contains path (see config.yaml) to the json file
        for k, v in kwargs.items():
            setattr(self, k, v)
            
        # Now load the data (article_id_fixed is the history, generated using truncate history)
        COLUMNS = ['user_id', 'article_id_fixed', 'article_ids_inview', 'article_ids_clicked', 'impression_id']
        self.load_data(COLUMNS)
        
        # Now tokenize the data and create the lookup tables
        self.tokenize_data()
        
        # Now create the X and y
        self.X = self.df_behaviors.drop('labels').with_columns(
            pl.col('article_ids_inview').list.len().alias('n_samples')
        ) #Drop labels and add n_samples (which is the number of articles in the inview list)
        self.y = self.df_behaviors['labels']
        
        # Lastly transform the data to get tokens and the right format for the model using the lookup tables
        (self.his_input_title, self.pred_input_title), self.y = self.transform()
        
    
    def __len__(self):
        return int(len(self.y))

    def __getitem__(self, idx):
        """
        his_input_title:    (samples, history_size, document_dimension)
        pred_input_title:   (samples, npratio, document_dimension)
        batch_y:            (samples, npratio)
        """
        x = (self.his_input_title[idx], self.pred_input_title[idx])
        y = self.y[idx]
        return x, y

    
    def load_data(self, COLUMNS):
        FULL_PATH = os.path.join(self.data_dir, self.split)
        
        # Load the data
        df_history = (
            pl.scan_parquet(os.path.join(FULL_PATH, 'history.parquet'))
            .pipe(
                truncate_history,
                column='article_id_fixed',
                history_size=self.history_size,
                padding_value=0,
                enable_warning=False,
            )
        )
        
        # Combine the behaviors and history data
        df_behaviors = (
            pl.scan_parquet(os.path.join(FULL_PATH, 'behaviors.parquet'))
            .collect()
            .pipe(
                slice_join_dataframes,
                df2=df_history.collect(),
                on='user_id',
                how='left',
            )
        )
        
        # Now transform the data for negative sampling and add labels based on train, val, test
        if self.neg_sampling and self.split == 'train':
            df_behaviors = df_behaviors.select(COLUMNS).pipe(
                sampling_strategy_wu2019,
                npratio=self.npratio,
                shuffle=True,
                with_replacement=True,
                seed=123,
            ).pipe(create_binary_labels_column).sample(fraction=self.dataset_fraction)
        else:
            df_behaviors = df_behaviors.select(COLUMNS).pipe(create_binary_labels_column).sample(fraction=self.dataset_fraction)
        
        # Store the behaviors in the class
        self.df_behaviors = df_behaviors
        
        # Load the article data
        self.df_articles = pl.read_parquet(os.path.join(self.data_dir, 'articles.parquet'))
        
    def tokenize_data(self):
        # This concatenates the title with the subtitle in the DF, the cat_cal is the column name
        #TODO: JE: Maybe also add subtitle for prediction
        #df_articles, cat_cal = concat_str_columns(df = self.df_articles, columns=['subtitle', 'title'])
        
        # This add the bert tokenization to the df
        self.df_articles, token_col_title = convert_text2encoding_with_transformers(self.df_articles, self.tokenizer, column='title', max_length=self.max_title_length)
        # Now create lookup tables
        article_mapping = create_article_id_to_value_mapping(df=self.df_articles, value_col=token_col_title)
        self.lookup_article_index, self.lookup_article_matrix = create_lookup_objects(
            article_mapping, unknown_representation='zeros'
        )
        self.unknown_index = [0]
        
        
    def transform(self):
        # Map the article ids to the lookup table (not sure what this value should represent, I think it's the tokenized title)
        self.X = self.X.pipe(
            map_list_article_id_to_value,
            behaviors_column='article_id_fixed',
            mapping=self.lookup_article_index,
            fill_nulls=self.unknown_index,
            drop_nulls=False,
        ).pipe(
            map_list_article_id_to_value,
            behaviors_column='article_ids_inview',
            mapping=self.lookup_article_index,
            fill_nulls=self.unknown_index,
            drop_nulls=False,
        )
        
        if self.eval_mode or not self.neg_sampling:
            repeats = np.array(self.X["n_samples"])
            # =>
            self.y = np.array(self.y.explode().to_list()).reshape(-1, 1)
            # =>
            his_input_title = repeat_by_list_values_from_matrix(
                self.X['article_id_fixed'].to_list(),
                matrix=self.lookup_article_matrix,
                repeats=repeats,
            )
            # =>
            pred_input_title = self.lookup_article_matrix[
                self.X['article_ids_inview'].explode().to_list()
            ]
        else:
            self.y = np.array(self.y.to_list())
            his_input_title = self.lookup_article_matrix[
                self.X['article_id_fixed'].to_list()
            ]
            pred_input_title = self.lookup_article_matrix[
                self.X['article_ids_inview'].to_list()
            ]
            pred_input_title = np.squeeze(pred_input_title, axis=2)

        his_input_title = np.squeeze(his_input_title, axis=2)
        
        return (his_input_title, pred_input_title), self.y
        
        
    
    
    # def encode(self, batch):
    #     batch = [tokenize(sent) for sent in batch]
    #     token_item = self.tokenizer(batch, padding="max_length", truncation=True, max_length=self.max_length, add_special_tokens=True)
    #     return token_item['input_ids'], token_item['attention_mask']

    # def format_srt(self, str_input):
    #     return str_input.replace('"', '')

    # def normal_sample(self, input, length):
    #     n_padding = len(input[-1])
    #     n_extending = length - len(input)        
    #     tokens = input + ([[0] * n_padding] * n_extending)
    #     return tokens[:length]