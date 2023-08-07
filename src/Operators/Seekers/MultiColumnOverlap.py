from src.Operators.OperatorBase import Operator
import numpy as np
from heapq import heapify, heappush, heappop
from src.utils import calculate_xash


class MultiColumnOverlap(Operator):
    def __init__(self, input_df, k=10):
        Operator.__init__(self)
        self.type = 'multicolumnintersection'
        self.input = input_df
        self.k = k
        self.base_sql = ' $INIT$ ' + f'SELECT firstcolumn.TableId, firstcolumn.RowId, firstcolumn.super_key, firstcolumn.CellValue, firstcolumn.ColumnId $OTHER_SELECT_COLUMNS$ FROM (SELECT TableId, RowId, CellValue, ColumnId, TO_BITSTRING(super_key) AS super_key FROM AllTables WHERE CellValue ' \
                        f'IN ($TOKENS$) $ADDITIONALS$ ) AS firstcolumn $INNERJOINS$'

    def create_sql_query(self):
        self.sql = self.base_sql.replace('$TOKENS$', self.create_sql_where_condition_from_value_list(self.clean_value_collection(self.input[self.input.columns.values[0]])))\
            .replace('$TOPK$', f'{self.k}')

        innerjoins = ''
        for column_index in np.arange(1, len(self.input.columns.values)):
            innerjoins += f' INNER JOIN (SELECT TableId, RowId, CellValue, ColumnId FROM AllTables WHERE CellValue ' \
                          f'IN ({self.create_sql_where_condition_from_value_list(self.clean_value_collection(self.input[self.input.columns.values[column_index]]))}) $ADDITIONALS$ ) clm_{self.input.columns.values[column_index]}   ' \
                          f'ON firstcolumn.TableId = clm_{self.input.columns.values[column_index]}.TableID AND firstcolumn.RowId = clm_{self.input.columns.values[column_index]}.RowId'
            self.sql = self.sql.replace('$OTHER_SELECT_COLUMNS$',
                                            f' , clm_{self.input.columns.values[column_index]}.CellValue, clm_{self.input.columns.values[column_index]}.ColumnId $OTHER_SELECT_COLUMNS$ ')
        
        self.sql = self.sql.replace('$INNERJOINS$', innerjoins).replace('$HAVING$', '').replace('$INIT$', '').replace('$ADDITIONALS$', '').replace('$OTHER_SELECT_COLUMNS$', '')

    def optimize(self, by, set_operation_type, create_executatable_query = False): # by, is an operator that we would like to optimize the sql based on
        linear = False  # If False means that the optimization merged the nodes and we don't need to linearly connect them together
        if set_operation_type == 'set_intersection':
            if by.type == 'intersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$ADDITIONALS$', f' AND TableId IN ($PREVIOUSSTEP_MUST$) $ADDITIONALS$')
                self.k = min(self.k, by.k)
                linear = True
            elif by.type == 'multicolumnintersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$ADDITIONALS$', f' AND TableId IN ($PREVIOUSSTEP_MUST$) $ADDITIONALS$') # Also try if the first one is run and only the table ids are sent
                linear = True
            elif by.type == 'quadrantapproximation':
                by.optimize(self, set_operation_type, False)
        elif set_operation_type == 'set_union':
            if by.type == 'intersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$INIT$', f' $INIT$ {by.sql} UNION DISTINCT ')
                self.k = self.k + by.k
            elif by.type == 'multicolumnintersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$INIT$', f' $INIT$ {by.sql} UNION DISTINCT ')
                self.k = self.k + by.k
            elif by.type == 'quadrantapproximation':
                self.base_sql = by.optimize(self, set_operation_type, False).base_sql
        elif set_operation_type == 'set_difference':
            if by.type == 'intersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$ADDITIONALS$', f' AND TableId NOT IN ($PREVIOUSSTEP_MUST$) $ADDITIONALS$')
            elif by.type == 'multicolumnintersection':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$ADDITIONALS$', f' AND TableId NOT IN ($PREVIOUSSTEP_MUST$) $ADDITIONALS$')
            elif by.type == 'quadrantapproximation':
                by.create_sql_query()
                self.base_sql = self.base_sql.replace('$ADDITIONALS$', f' AND TableId NOT IN ($PREVIOUSSTEP_MUST$) $ADDITIONALS$')
            linear = True
        if create_executatable_query:
            self.create_sql_query()
        return linear
    

    def run(self, PLs, DB):
        PL_dictionary = {}
        PL_candidate_structure = {}
        for tablerow_superkey in PLs:
            # table_row = tablerow_superkey[0]
            table = tablerow_superkey[0]
            row = tablerow_superkey[1]
            superkey = tablerow_superkey[2]
            token = tablerow_superkey[3]
            colid = tablerow_superkey[4]
            tokens = [tablerow_superkey[x] for x in np.arange(5, len(tablerow_superkey), 2)]
            cols = [tablerow_superkey[x] for x in np.arange(6, len(tablerow_superkey), 2)]
            if table in PL_dictionary:
                PL_dictionary[table] += [(row, superkey, token, colid)]
            else:
                PL_dictionary[table] = [(row, superkey, token, colid)]
            PL_candidate_structure[(table, row)] = [tokens, cols]

        top_joinable_tables = []  # each item includes: Tableid, joinable_rows
        heapify(top_joinable_tables)
        query_columns = self.input.columns.values
        self.input['SuperKey'] = self.input.apply(lambda row: self.hash_row_vals(row), axis=1)

        g = self.input.groupby([self.input.columns.values[0]])
        gd = {}
        for key, item in g:
            gd[str(key[0])] = np.array(g.get_group(key[0]))

        candidate_external_row_ids = []
        candidate_external_col_ids = []
        candidate_input_rows = []
        candidate_table_rows = []
        candidate_table_ids = []
        all_pls = 0
        total_approved = 0
        total_match = 0
        overlaps_dict = {}
        super_key_index = list(self.input.columns.values).index('SuperKey')
        checked_tables = 0
        max_table_check = 10000000
        for tableid in sorted(PL_dictionary, key=lambda k: len(PL_dictionary[k]), reverse=True)[:max_table_check]:
            checked_tables += 1
            if checked_tables == max_table_check:
                # pruned = True
                break
            set_of_rowids = set()
            hitting_PLs = PL_dictionary[tableid]
            if len(top_joinable_tables) >= self.k and top_joinable_tables[0][0] >= len(hitting_PLs):
                # pruned = True
                break
            already_checked_hits = 0
            for hit in sorted(hitting_PLs):
                if len(top_joinable_tables) >= self.k and (
                        (len(hitting_PLs) - already_checked_hits + len(set_of_rowids)) <
                        top_joinable_tables[0][0]):
                    break
                rowid = hit[0]
                superkey = int(hit[1], 2)
                token = hit[2]
                colid = hit[3]
                relevant_input_rows = gd[token]
                for input_row in relevant_input_rows:
                    all_pls += 1
                    if (input_row[super_key_index] | superkey) == superkey:
                        candidate_external_row_ids += [rowid]
                        set_of_rowids.add(rowid)
                        candidate_external_col_ids += [colid]
                        candidate_input_rows += [input_row]
                        candidate_table_ids += [tableid]
                        candidate_table_rows += [f'{tableid}_{rowid}']
                already_checked_hits += 1
        if len(candidate_external_row_ids) > 0:
            candidate_input_rows = np.array(candidate_input_rows)
            candidate_table_ids = np.array(candidate_table_ids)
            

            joint_distinct_values = '\',\''.join(candidate_table_rows)
            joint_distinct_rows = '\',\''.join(set([str(x) for x in candidate_external_row_ids]))
            joint_distinct_tableids = '\',\''.join(set([str(x) for x in candidate_table_ids]))
            query = 'SELECT CONCAT(CONCAT(TableId, \'_\'), RowId), ColumnId, CellValue FROM (SELECT * from AllTables WHERE TableId in (\'{}\') and RowId in (\'{}\')) AS intermediate WHERE CONCAT(CONCAT(TableId, \'_\'), RowId) IN (\'{}\');'.format(
                joint_distinct_tableids, joint_distinct_rows, joint_distinct_values)

            pls_to_evaluate, execution_time, fetch_time = DB.execute_and_fetchall(query)
            table_row_dict = {}  # contains rowid that each rowid has dict that maps colids to tokenized
            for i in pls_to_evaluate:
                if i[0] not in table_row_dict:
                    table_row_dict[str(i[0])] = {}
                    table_row_dict[str(i[0])][str(i[1])] = str(i[2])
                else:
                    table_row_dict[str(i[0])][str(i[1])] = str(i[2])

            
            for i in np.arange(len(candidate_table_rows)):
                if str(candidate_table_rows[i]) not in table_row_dict:
                    continue
                col_dict = table_row_dict[str(candidate_table_rows[i])]
                match, matched_columns = self.evaluate_rows(candidate_input_rows[i], col_dict, query_columns)
                total_approved += 1
                if match:
                    total_match += 1
                    complete_matched_columns = '{}{}'.format(str(candidate_external_col_ids[i]), matched_columns)
                    if candidate_table_ids[i] not in overlaps_dict:
                        overlaps_dict[candidate_table_ids[i]] = {}

                    if complete_matched_columns in overlaps_dict[candidate_table_ids[i]]:
                        overlaps_dict[candidate_table_ids[i]][complete_matched_columns] += 1
                    else:
                        overlaps_dict[candidate_table_ids[i]][complete_matched_columns] = 1
            for tbl in set(candidate_table_ids):
                if tbl in overlaps_dict and len(overlaps_dict[tbl]) > 0:
                    join_keys = max(overlaps_dict[tbl], key=overlaps_dict[tbl].get)
                    joinability_score = overlaps_dict[tbl][join_keys]
                    if self.k <= len(top_joinable_tables):
                        if top_joinable_tables[0][0] < joinability_score:
                            popped_table = heappop(top_joinable_tables)
                            heappush(top_joinable_tables, [joinability_score, tbl, join_keys])
                    else:
                        heappush(top_joinable_tables, [joinability_score, tbl, join_keys])
        print(f'All: {all_pls}, approaved:{total_approved}, match:{total_match}')
        return [(tableid, ) for _, tableid, _ in top_joinable_tables[::-1]]

    def hash_row_vals(self, row):
        hresult = 0
        for q in row:
            hvalue = calculate_xash(str(q))
            hresult = hresult | hvalue
        return hresult
    

    def evaluate_rows(self, input_row, col_dict, query_columns):
        vals = list(col_dict.values())
        query_cols_arr = np.array(query_columns)
        query_degree = len(query_cols_arr)
        matching_column_order = ''
        for q in query_cols_arr[-(query_degree - 1):]:
            q_index = list(query_columns).index(q)
            if input_row[q_index] not in vals:
                return False, ''
            else:
                for colid, val in col_dict.items():
                    if val == input_row[q_index]:
                        matching_column_order += '_{}'.format(str(colid))
        return True, matching_column_order
    