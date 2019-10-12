from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import combinations

import networkx as nx
import numpy as np
import pandas as pd
from networkx.algorithms.centrality import edge_betweenness_centrality
from numpy import log
from scipy.special import betaln


def extract_all_nodes(linkage, labels=None):
    """
    Given a linkage array output from scipy.cluster.hierarchy.linkage,
    calculate the left and right branches for all of the non-singleton nodes.
    """

    cluster_dict = {}
    cur_cluster_id = len(linkage) + 1
    total_obs = len(linkage) + 1
    if labels is None:
        labels = list(range(total_obs))
    for left, right, distance, n_obs in linkage:
        left = int(left)
        right = int(right)
        n_obs = int(n_obs)

        cluster_dict[cur_cluster_id] = {'left': set(),
                                        'right': set()}
        if n_obs == 2:
            left = labels[left]
            right = labels[right]
            # merge of 2 original observations
            cluster_dict[cur_cluster_id]['left'].add(left)
            cluster_dict[cur_cluster_id]['right'].add(right)
        else:
            # left and/or right are cluster
            if left < total_obs:
                left = labels[left]
                cluster_dict[cur_cluster_id]['left'].add(left)
            else:
                # node are cluster
                cluster_dict[cur_cluster_id]['left'].update(cluster_dict[left]['left'])
                cluster_dict[cur_cluster_id]['left'].update(cluster_dict[left]['right'])
            if right < total_obs:
                right = labels[right]
                cluster_dict[cur_cluster_id]['right'].add(right)
            else:
                # node are cluster
                cluster_dict[cur_cluster_id]['right'].update(cluster_dict[right]['left'])
                cluster_dict[cur_cluster_id]['right'].update(cluster_dict[right]['right'])
        cur_cluster_id += 1
    return cluster_dict


def dmr_distance(a: np.ndarray, b: np.ndarray):
    overlap = (a == b)[(a * b) != 0]
    return 1 - (overlap.sum() / overlap.size)


def linkage_to_graph(linkage):
    """Turn the linkage matrix into a graph, an epimutation will just be remove one edge from the graph"""
    _linkage = linkage.astype(int)
    n_leaf = _linkage.shape[0] + 1
    edges = []
    for i in range(_linkage.shape[0]):
        cur_node = i + n_leaf
        left, right, *_ = _linkage.iloc[i]
        edges.append([left, cur_node])
        edges.append([right, cur_node])
    g = nx.Graph()
    g.add_edges_from(edges)
    return g


def cut_by_highest_betweenness_centrality(g):
    # order graph node by betweenness_centrality
    highest_centrality_edge = pd.Series(edge_betweenness_centrality(g)).sort_values(ascending=False).index[0]
    _g = g.copy()
    _g.remove_edge(*highest_centrality_edge)
    left_tree, right_tree = nx.connected_component_subgraphs(_g)
    return left_tree, right_tree, highest_centrality_edge


def log_proba_beta_binomial(x, n, a, b):
    """log likelihood for the beta-binomial dist, ignore part not related to a and b."""
    like = betaln((a + x), (b + n - x)) - betaln(a, b)
    # when a or b has 0, like will have nan
    return like.fillna(0)


def parse_one_pattern(tree_g, edges_to_remove, mc_df, cov_df):
    """
    for a particular epimutation combination (edges_to_remove),
    calculate the a and b for beta-binomial dist in each leaf node group.

    after removing the edges (epimutations),
    the leaf node group are leaf nodes in each of the disconnected sub graph.
    """
    group_mc_df = mc_df.copy()
    group_un_mc_df = cov_df - group_mc_df

    sub_g = tree_g.copy()
    if len(edges_to_remove) > 0:  # this is the case of adding empty edge by left-right combine
        sub_g.remove_edges_from(edges_to_remove)
    # get disconnected sub-graphs
    sub_tree = nx.connected_component_subgraphs(sub_g)

    # for each sub-graph, add up the mc and un-mc of all leaf nodes for group a, b in beta-binomial dist
    for _tree in sub_tree:
        judge = group_mc_df.columns.isin(_tree.nodes)
        if judge.sum() == 0:
            # if sub-graph do not have leaf nodes, skip this sub-graph
            continue
        group_mc_df.loc[:, judge] = group_mc_df.loc[:, judge].sum(
            axis=1).values[:, None]
        group_un_mc_df.loc[:, judge] = group_un_mc_df.loc[:, judge].sum(
            axis=1).values[:, None]

    # group_mc_df is a, group_un_mc_df is b for beta-binomial dist
    # each group of leaf nodes share same a, b
    return group_mc_df, group_un_mc_df


def mutation_likelihood(n_mutation, p_mutation, n_edges):
    lp0 = n_mutation * log(p_mutation) + \
          (n_edges - n_mutation) * log(1 - p_mutation)
    return lp0


def _max_likelihood_tree_worker(tree_g, mc_df, cov_df, max_mutation=2, p_mutation=0.1, sub_tree_cutoff=12):
    top_n = 1

    n_edges = len(tree_g.edges)
    max_mutation = min(n_edges, max_mutation)

    record_names = mc_df.index

    if n_edges > sub_tree_cutoff:
        # cut the tree into left and right in the edge that has biggest betweenness_centrality
        # calculate best patterns for left and right separately, and then joint consider the overall pattern
        left_tree, right_tree, removed_edge = cut_by_highest_betweenness_centrality(tree_g)
        left_best_patterns, _ = _max_likelihood_tree_worker(
            left_tree,
            mc_df=mc_df.loc[:, mc_df.columns.isin(left_tree.nodes)],
            cov_df=cov_df.loc[:, cov_df.columns.isin(left_tree.nodes)],
            max_mutation=max_mutation, p_mutation=p_mutation, sub_tree_cutoff=sub_tree_cutoff)
        right_best_patterns, _ = _max_likelihood_tree_worker(
            right_tree,
            mc_df=mc_df.loc[:, mc_df.columns.isin(right_tree.nodes)],
            cov_df=cov_df.loc[:, cov_df.columns.isin(right_tree.nodes)],
            max_mutation=max_mutation, p_mutation=p_mutation, sub_tree_cutoff=sub_tree_cutoff)

        # for each DMR, go through all possible combination of best left and right pattern,
        # when not exceed max_mutation, also consider whether should we add the removed edge or not
        best_pattern_final = {}
        likelihood_final = {}
        for record_name in record_names:
            _this_mc_df = mc_df.loc[[record_name]]
            _this_cov_df = cov_df.loc[[record_name]]

            left_patterns = list(left_best_patterns[record_name]) + [()]  # add empty choice
            right_patterns = list(right_best_patterns[record_name]) + [()]  # add empty choice
            middle_patterns = [[removed_edge], []]

            # list all possible combined patterns
            pattern_dict = {}
            for left_i, left_pattern in enumerate(left_patterns):
                for right_i, right_pattern in enumerate(right_patterns):
                    for middle_pattern in middle_patterns:
                        joint_pattern = (list(left_pattern) if len(left_pattern) != 0 else []) + (
                            list(right_pattern) if len(right_pattern) != 0 else []) + (
                                            list(middle_pattern) if len(middle_pattern) != 0 else [])
                        _n_mutation = len(joint_pattern)
                        if _n_mutation > max_mutation:
                            continue

                        _this_group_mc_df, _this_group_un_mc_df = parse_one_pattern(
                            tree_g, joint_pattern, _this_mc_df, _this_cov_df)

                        # calculate tree likelihood on current pattern for all DMR
                        dmr_tree_likelihood = log_proba_beta_binomial(
                            _this_mc_df, _this_cov_df, _this_group_mc_df, _this_group_un_mc_df).values.sum()
                        # add mutation prior to tree likelihood, save to records
                        lp0 = mutation_likelihood(_n_mutation, p_mutation, n_edges)
                        try:
                            pattern_dict[_n_mutation][tuple(joint_pattern)] = dmr_tree_likelihood + lp0
                        except KeyError:
                            pattern_dict[_n_mutation] = {tuple(joint_pattern): dmr_tree_likelihood + lp0}
            _this_final_pattern = []
            _this_final_likelihood = []
            for _n_mutation, _n_mutation_patterns in pattern_dict.items():
                if _n_mutation != 0:
                    _s = pd.Series(_n_mutation_patterns).sort_values(ascending=False)[:top_n]
                    _this_final_pattern += _s.index.tolist()
                    _this_final_likelihood += _s.tolist()
                else:
                    # empty pattern
                    _this_final_pattern += [()]
                    _this_final_likelihood += list(_n_mutation_patterns.values())

            best_pattern_final[record_name] = np.array(_this_final_pattern)
            likelihood_final[record_name] = np.array(_this_final_likelihood)
        return pd.Series(best_pattern_final), pd.Series(likelihood_final)

    else:
        records = {}
        mutation_patterns = {}
        for n_mutation in range(1, max_mutation + 1):
            # Prior probability of the mutations, which is same for each n_mutation
            lp0 = n_mutation * log(p_mutation) + \
                  (n_edges - n_mutation) * log(1 - p_mutation)

            # each epimutation is removing one edge from the graph
            # for N epimutation, the result graph contain N + 1 disconnected sub-graph
            for i, edges in enumerate(combinations(tree_g.edges, n_mutation)):
                # get a and b for beta-binomial dist
                group_mc_df, group_un_mc_df = parse_one_pattern(tree_g, edges, mc_df, cov_df)

                # calculate tree likelihood on current pattern for all DMR
                dmr_tree_likelihood = log_proba_beta_binomial(mc_df, cov_df,
                                                              group_mc_df, group_un_mc_df).sum(axis=1)
                # add mutation prior to tree likelihood, save to records
                records[(n_mutation, i)] = dmr_tree_likelihood + lp0
                mutation_patterns[(n_mutation, i)] = edges

        # records_df: each row is a DMR record, each column is a (n_mutation, mutation_pattern_idx)
        records_df = pd.DataFrame(records)
        # mutation_pattern_series, index is (n_mutation, mutation_pattern_idx), value is the actual mutation pattern
        mutation_pattern_series = pd.Series(mutation_patterns)

        def __get_row_best_patterns(_row):
            _row_best_patterns = []
            _row_best_likelihoods = []
            for group, sub_row in _row.groupby(_row.index.get_level_values(0)):
                # index is pattern id, value is likelihood
                selected_pattern = sub_row.sort_values(ascending=False)[:top_n]
                _row_best_patterns.append(mutation_pattern_series.loc[selected_pattern.index])
                _row_best_likelihoods.append(selected_pattern)
            return pd.concat(_row_best_patterns).values, pd.concat(_row_best_likelihoods).values

        # top_n candidate pattern for each n_mutation
        patten_dict = {}
        likelihood_dict = {}
        for record_name, row in records_df.iterrows():
            _best_patterns, _likelihoods = __get_row_best_patterns(row)
            patten_dict[record_name] = _best_patterns
            likelihood_dict[record_name] = _likelihoods
        return pd.Series(patten_dict), pd.Series(likelihood_dict)


def dmr_parsimony_fit(linkage, mc_df, cov_df, max_mutation=5, p_mutation=0.1, sub_tree_cutoff=12, cpu=1):
    tree_g = linkage_to_graph(linkage)

    chunk_size = int(min(mc_df.shape[0], 10))
    futures = {}
    with ProcessPoolExecutor(cpu) as executor:
        for chunk_start in range(0, mc_df.shape[0], chunk_size):
            _chunk_mc_df = mc_df.iloc[chunk_start:chunk_start + chunk_size, :].copy()
            _chunk_cov_df = cov_df.iloc[chunk_start:chunk_start + chunk_size, :].copy()
            future = executor.submit(_max_likelihood_tree_worker,
                                     tree_g=tree_g,
                                     mc_df=_chunk_mc_df,
                                     cov_df=_chunk_cov_df,
                                     max_mutation=max_mutation,
                                     p_mutation=p_mutation,
                                     sub_tree_cutoff=sub_tree_cutoff)
            futures[future] = chunk_start

    results_dict = {}
    for future in as_completed(futures):
        chunk_start = futures[future]
        result = future.result()
        results_dict[chunk_start] = result

    dmr_best_mutations_list = []
    dmr_likelihoods_list = []
    for chunk_start in range(0, mc_df.shape[0], chunk_size):
        _patterns, _likelihoods = results_dict[chunk_start]
        dmr_best_mutations_list.append(_patterns)
        dmr_likelihoods_list.append(_likelihoods)
    total_records = pd.DataFrame({'mutation': pd.concat(dmr_best_mutations_list),
                                  'likelihoods': pd.concat(dmr_likelihoods_list)})
    best_choice = total_records.apply(lambda row: row['mutation'][row['likelihoods'].argmax()], axis=1)
    best_likelihood = total_records['likelihoods'].apply(lambda i: i.argmax())
    return best_choice, best_likelihood
