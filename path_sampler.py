import argparse
import json
import os
from difflib import get_close_matches
import itertools

import torch
import numpy as np
import time
import rule_application as ra

from data import *
from multiprocessing import Pool
from functools import partial
from tqdm import tqdm
from grapher import Grapher
from rule_learning import Rule_Learner, rules_statistics
from temporal_walk import Temporal_Walk
from joblib import Parallel, delayed
from datetime import datetime



os.environ['CURL_CA_BUNDLE'] = ''

from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity

from utils import load_json_data, save_json_data, construct_adjacency_list_and_index, subgraph_extraction_labeling, \
    calculate_hours_between_dates_pandas


def sample_paths(max_path_len, anchor_num, fact_rdf, entity2desced, rdict, cores, output_path):
    print("Sampling training data...")
    print("Number of head relation:{}".format((rdict.__len__() - 1) // 2))
    # print("Maximum paths per head: {}".format(anchor_num))
    fact_dict = construct_fact_dict(fact_rdf)
    with open(os.path.join(output_path, "closed_rel_paths.jsonl"), "w") as f:
        for head in tqdm(rdict.rel2idx):
            paths = set()
            if head == "None" or "inv_" in head:
                continue
            # Sample anchor
            sampled_rdf = sample_anchor_rdf(fact_dict[head], num=anchor_num)
            with Pool(cores) as p:
                for path_seq in p.map(
                        partial(search_closed_rel_paths, entity2desced=entity2desced, max_path_len=max_path_len),
                        sampled_rdf):
                    paths = paths.union(set(path_seq))
            paths = list(paths)
            tqdm.write("Head relation: {}".format(head))
            tqdm.write("Number of paths: {}".format(len(paths)))
            tqdm.write("Saving paths...")
            json.dump({"head": head, "paths": paths}, f)
            f.write("\n")
            f.flush()


def select_similary_relations(relation2id, output_dir):
    id2relation = dict([(v, k) for k, v in relation2id.items()])

    save_json_data(id2relation, os.path.join(output_dir, 'transfomers_id2rel.json'))
    save_json_data(relation2id, os.path.join(output_dir, 'transfomers_rel2id.json'))

    all_rels = list(relation2id.keys())
    # 加载预训练的模型
    model = SentenceTransformer('bert-base-nli-mean-tokens')

    # 定义句子
    sentences_A = all_rels
    sentences_B = all_rels

    # 使用模型为句子编码
    embeddings_A = model.encode(sentences_A)
    embeddings_B = model.encode(sentences_B)

    # 计算句子之间的余弦相似度
    similarity_matrix = cosine_similarity(embeddings_A, embeddings_B)

    np.fill_diagonal(similarity_matrix, 0)

    np.save(os.path.join(output_dir, 'matrix.npy'), similarity_matrix)

def main(parsed):
    dataset = parsed["dataset"]
    rule_lengths = parsed["max_path_len"]
    rule_lengths = (torch.arange(rule_lengths) + 1).tolist()
    num_walks = parsed["num_walks"]
    transition_distr = parsed["transition_distr"]
    num_processes = parsed["cores"]
    seed = parsed["seed"]
    version_id = parsed["version"]

    dataset_dir = "./datasets/" + dataset + "/"
    data = Grapher(dataset_dir)

    if version_id == 'train_valid':
        temporal_walk = Temporal_Walk(np.array(data.train_idx.tolist() + data.valid_idx.tolist()), data.inv_relation_id,
                                      transition_distr)
    elif version_id == 'train':
        temporal_walk = Temporal_Walk(np.array(data.train_idx.tolist()), data.inv_relation_id,
                                      transition_distr)
    elif version_id == 'test':
        temporal_walk = Temporal_Walk(np.array(data.test_idx.tolist()), data.inv_relation_id,
                                      transition_distr)
    elif version_id == 'valid':
        temporal_walk = Temporal_Walk(np.array(data.valid_idx.tolist()), data.inv_relation_id,
                                      transition_distr)

    rl = Rule_Learner(temporal_walk.edges, data.id2relation, data.inv_relation_id, dataset)
    all_relations = sorted(temporal_walk.edges)  # Learn for all relations
    all_relations = [int(item) for item in all_relations]
    rel2idx = data.relation2id

    select_similary_relations(data.relation2id, rl.output_dir)

    constant_config = load_json_data('./Config/constant.json')
    relation_regex = constant_config['relation_regex'][dataset]

    # relation_graph_file = f'relation_subgraph.json'
    # relation_graph_file_path = os.path.join(rl.output_dir, relation_graph_file)
    #
    # if not os.path.exists(relation_graph_file_path):
    #     num_entities = len(data.entity2id.keys())
    #     adj_list, relation_index = construct_adjacency_list_and_index(
    #         (data.train_idx.tolist() + data.valid_idx.tolist()),
    #         list(data.relation2id.values()), num_entities)
    #     gen_subgraph_based_relation(adj_list, all_relations, data, num_processes, relation_graph_file_path, seed)

    def learn_rules(i, num_relations):
        """
        Learn rules (multiprocessing possible).

        Parameters:
            i (int): process number
            num_relations (int): minimum number of relations for each process

        Returns:
            rl.rules_dict (dict): rules dictionary
        """

        print("Start process", i, "...")

        if seed:
            np.random.seed(seed)

        if i < num_processes - 1:
            relations_idx = range(i * num_relations, (i + 1) * num_relations)
        else:
            relations_idx = range(i * num_relations, len(all_relations))

        num_rules = [0]
        for k in relations_idx:
            rel = all_relations[k]
            for length in rule_lengths:
                it_start = time.time()
                for _ in range(num_walks):
                    walk_successful, walk = temporal_walk.sample_walk(length + 1, rel)
                    if walk_successful:
                        rl.create_rule(walk)
                it_end = time.time()
                it_time = round(it_end - it_start, 6)
                num_rules.append(sum([len(v) for k, v in rl.rules_dict.items()]) // 2)
                num_new_rules = num_rules[-1] - num_rules[-2]
                print(
                    "Process {0}: relation {1}/{2}, length {3}: {4} sec, {5} rules".format(
                        i,
                        k - relations_idx[0] + 1,
                        len(relations_idx),
                        length,
                        it_time,
                        num_new_rules,
                    )
                )

        return rl.rules_dict

    def learn_rules_with_relax_time(i, num_relations):
        """
        Learn rules (multiprocessing possible).

        Parameters:
            i (int): process number
            num_relations (int): minimum number of relations for each process

        Returns:
            rl.rules_dict (dict): rules dictionary
        """

        if seed:
            np.random.seed(seed)

        if i < num_processes - 1:
            relations_idx = range(i * num_relations, (i + 1) * num_relations)
        else:
            relations_idx = range(i * num_relations, len(all_relations))

        num_rules = [0]
        for k in relations_idx:
            rel = all_relations[k]
            for length in rule_lengths:
                it_start = time.time()
                for _ in range(num_walks):
                    walk_successful, walk = temporal_walk.sample_walk_with_relax_time(length + 1, rel)
                    if walk_successful:
                        rl.create_rule_with_relax_time(walk)
                it_end = time.time()
                it_time = round(it_end - it_start, 6)
                num_rules.append(sum([len(v) for k, v in rl.rules_dict.items()]) // 2)
                num_new_rules = num_rules[-1] - num_rules[-2]
                print(
                    "Process {0}: relation {1}/{2}, length {3}: {4} sec, {5} rules".format(
                        i,
                        k - relations_idx[0] + 1,
                        len(relations_idx),
                        length,
                        it_time,
                        num_new_rules,
                    )
                )

        return rl.rules_dict

    if parsed['is_relax_time'] is False:
        start = time.time()
        num_relations = len(all_relations) // num_processes
        output = Parallel(n_jobs=num_processes)(
            delayed(learn_rules)(i, num_relations) for i in range(num_processes)
        )
        end = time.time()
        all_graph = output[0]
        for i in range(1, num_processes):
            all_graph.update(output[i])

        total_time = round(end - start, 6)
        print("Learning finished in {} seconds.".format(total_time))

    else:

        start = time.time()
        num_relations = len(all_relations) // num_processes
        output = Parallel(n_jobs=num_processes)(
            delayed(learn_rules_with_relax_time)(i, num_relations) for i in range(num_processes)
        )
        end = time.time()
        all_graph = output[0]
        for i in range(1, num_processes):
            all_graph.update(output[i])

        total_time = round(end - start, 6)
        print("Learning finished in {} seconds.".format(total_time))

    rl.rules_dict = all_graph
    rl.sort_rules_dict()
    dt = datetime.now()
    dt = dt.strftime("%d%m%y%H%M%S")
    rl.save_rules(dt, rule_lengths, num_walks, transition_distr, seed)
    save_json_data(rl.rules_dict, rl.output_dir + 'confidence.json')
    rules_statistics(rl.rules_dict)
    rl.save_rules_verbalized(dt, rule_lengths, num_walks, transition_distr, seed, rel2idx, relation_regex)


def gen_subgraph_based_relation(adj_list, all_relations, data, num_processes, relation_graph_file_path, seed):
    all_data = np.array(data.train_idx.tolist() + data.valid_idx.tolist())
    all_data = all_data[(all_data[:, 3] > 0)]
    relation_triple = {}
    for relation in data.id2relation.keys():
        mask = (all_data[:, 1] == relation) * (all_data[:, 0] != all_data[:, 2])
        triple_list = all_data[mask].tolist()
        relation_triple.setdefault(relation, []).extend(random.sample(triple_list, min(5, len(triple_list))))

    def gen_subgraph_for_multi_thread(i, num_relations, adj_list, data, relation_triple, seed):
        """
        Learn rules (multiprocessing possible).

        Parameters:
            i (int): process number
            num_relations (int): minimum number of relations for each process

        Returns:
            rl.rules_dict (dict): rules dictionary
        """

        if seed:
            np.random.seed(seed)

        num_rest_relations = len(all_relations) - (i + 1) * num_relations
        if num_rest_relations >= num_relations:
            relations_idx = range(i * num_relations, (i + 1) * num_relations)
        else:
            relations_idx = range(i * num_relations, len(all_relations))

        relation_subgraph = {}
        for idx in relations_idx:
            relation_id = all_relations[idx]
            gen_subgraph(adj_list, data, relation_triple[relation_id], relation_id, relation_subgraph)
            print(f"Process {i}: relation {idx - relations_idx[0] + 1}/{len(relations_idx)}")

        return relation_subgraph

    start = time.time()
    num_relations = len(all_relations) // num_processes
    output = Parallel(n_jobs=num_processes)(
        delayed(gen_subgraph_for_multi_thread)(i, num_relations, adj_list, data, relation_triple, seed) for i in
        range(num_processes)
    )
    end = time.time()
    all_graph = output[0]
    for i in range(1, num_processes):
        all_graph.update(output[i])
    total_time = round(end - start, 6)
    print("Generate Subgraph in {} seconds.".format(total_time))
    save_json_data(all_graph, relation_graph_file_path)


def gen_subgraph(adj_list, data, triples, relation_id, relation_subgraph):
    for triple in triples:
        ind = (triple[0], triple[2])
        ind_key = f'{triple[0]}_{triple[2]}'
        current_ts = triple[3]
        subgraph_edges = []
        temp_dict = {}
        subgraph_nodes = subgraph_extraction_labeling(ind, adj_list, kind="intersection", h=2, max_nodes_per_hop=50)
        for idx, subject_object in enumerate(subgraph_nodes):
            result = ra.get_window_edges_for_subject_object(
                np.array(data.train_idx.tolist() + data.valid_idx.tolist()),
                current_ts, subject_object, window=0)
            if len(result):
                result.append(triple)
                subgraph_edges.extend(result)

        if len(subgraph_edges):
            temp_dict.setdefault(ind_key, []).extend(subgraph_edges)
            relation_subgraph.setdefault(relation_id, []).append(temp_dict)


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    if value.lower() in ('yes', 'true', 't', 'y', '1'):
        return True
    elif value.lower() in ('no', 'false', 'f', 'n', '0'):
        return False
    else:
        raise argparse.ArgumentTypeError('Boolean value expected.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_path', type=str, default='datasets', help='data directory')
    parser.add_argument("--dataset", "-d", default="", type=str)
    parser.add_argument("--max_path_len", "-m", type=int, default=3, help="max sampled path length")
    parser.add_argument("--anchor", type=int, default=5, help="anchor facts for each relation")
    parser.add_argument("--output_path", type=str, default="sampled_path", help="output path")
    parser.add_argument("--sparsity", type=float, default=1, help="dataset sampling sparsity")
    parser.add_argument("--cores", "-p", type=int, default=5, help="dataset sampling sparsity")
    parser.add_argument("--num_walks", "-n", default="100", type=int)
    parser.add_argument("--transition_distr", default="exp", type=str)
    parser.add_argument("--seed", "-s", default=None, type=int)
    parser.add_argument("--window", "-w", default=0, type=int)
    parser.add_argument("--version", default="train", type=str,
                        choices=['train', 'test', 'train_valid', 'valid'])
    parser.add_argument("--is_relax_time", default='no', type=str_to_bool)

    parsed = vars(parser.parse_args())

    main(parsed)
