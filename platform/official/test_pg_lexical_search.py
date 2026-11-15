"""Synthetic PostgreSQL/FTS5 differential; SQLite is only an offline oracle.

Run unit tests: python -m unittest official.test_pg_lexical_search -v
PG fixtures require WUYU_PG_TEST_DSN naming wuyu_qa111_fixture* or qa111_fixture*.
The test creates/removes only its own qa111_lex_<uuid> schema. No model/network
provider, private configuration, application database or business file is read.
"""
import ast
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import types
import unittest
import uuid

from . import lexical_search as lexical


def memory_terms(text):
    # Execute the actual production function without importing app/DB clients.
    source = (Path(__file__).parent / 'memory.py').read_text(encoding='utf-8')
    tree = ast.parse(source)
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'terms')
    namespace = {'re': re}
    exec(compile(ast.Module([function], type_ignores=[]), 'actual-memory-terms', 'exec'), namespace)
    return namespace['terms'](text)


def phrase_frequency(tokens, phrase):
    return sum(tokens[i:i+len(phrase)] == phrase for i in range(len(tokens)))


def python_scores(corpus, query_terms):
    tokens = {key: lexical.tokenize_terms(text) for key, text in corpus.items()}
    phrases = [lexical.tokenize_terms(term) for term in query_terms]
    phrases = [phrase for phrase in phrases if phrase]
    if not tokens or not phrases:
        return {}
    average = sum(map(len, tokens.values())) / len(tokens)
    idfs = []
    for phrase in phrases:
        documents = sum(phrase_frequency(row, phrase) > 0 for row in tokens.values())
        value = math.log((len(tokens)-documents+0.5)/(documents+0.5))
        idfs.append(value if value > 0 else 1e-6)
    result = {}
    for key, row in tokens.items():
        frequency = [phrase_frequency(row, phrase) for phrase in phrases]
        if not any(frequency):
            continue
        score = 0.0
        for weight, count in zip(idfs, frequency):
            score += weight * ((count * (1.2+1.0))/(count+1.2*(1.0-0.75+0.75*len(row)/average)))
        result[key] = -score
    return result


def match_expression(query_terms):
    return ' OR '.join('"'+term.replace('"', '""')+'"' for term in query_terms)


class TokenizerTests(unittest.TestCase):
    def test_entire_current_CJK_domain_matches_unicode61(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute("CREATE VIRTUAL TABLE f USING fts5(terms,tokenize='unicode61')")
            conn.executemany('INSERT INTO f(rowid,terms) VALUES(?,?)', ((cp, chr(cp)) for cp in range(0x3400, 0xa000)))
            conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f,'instance')")
            oracle = {rowid: term for term, rowid in conn.execute('SELECT term,doc FROM v')}
            for cp in range(0x3400, 0xa000):
                self.assertEqual(lexical.tokenize_terms(chr(cp)), [oracle[cp]] if cp in oracle else [])

    def test_ascii_hyphens_underscores_and_CJK_boundaries(self):
        with sqlite3.connect(':memory:') as conn:
            conn.execute("CREATE VIRTUAL TABLE f USING fts5(terms,tokenize='unicode61')")
            conn.execute("CREATE VIRTUAL TABLE v USING fts5vocab(f,'instance')")
            samples = ['ABC_def-X2', 'alpha-beta alpha_beta alpha beta', '中文 化学 ䷀文化 ䷁', 'foo--bar__baz', ' A1_Z9  ']
            for text in samples:
                conn.execute('DELETE FROM f');conn.execute('INSERT INTO f(rowid,terms) VALUES(1,?)', (text,))
                expected = [row[0] for row in conn.execute('SELECT term FROM v ORDER BY offset')]
                self.assertEqual(lexical.tokenize_terms(text), expected)

    def test_unsupported_source_domain_refuses_without_runtime_SQLite(self):
        for text in ['café', 'a.b', 'alpha"beta', '\U0001f600', None]:
            with self.assertRaisesRegex(ValueError, '^Unsupported pre-segmented lexical index terms$'):
                lexical.tokenize_terms(text)
        imports = [node for node in ast.walk(ast.parse(Path(lexical.__file__).read_text(encoding='utf-8'))) if isinstance(node, (ast.Import, ast.ImportFrom))]
        self.assertFalse(any('sqlite' in ast.unparse(node) for node in imports))

    def test_query_is_parameterized_and_preserves_duplicate_normalized_phrases(self):
        sql, params = lexical.query_sql('memory', ['alpha-beta', 'alpha_beta'], ("user' OR true --", 'chat'), 4)
        self.assertNotIn("user'", sql)
        self.assertEqual(params[:4], (0, ['alpha','beta'], 1, ['alpha','beta']))
        self.assertEqual(params[-3:], ("user' OR true --", 'chat', 4))
        self.assertIn('SUM(value ORDER BY ordinal)', sql)
        self.assertIn('ORDER BY r.rank,c.created DESC,c.id DESC LIMIT ?', sql)
        self.assertEqual(lexical.query_sql('document', ['alpha'], ('u', []), 40), (None, ()))
        self.assertEqual(lexical.query_sql('memory', ['䷀'], ('u','c'), 40), (None, ()))
        with self.assertRaises(ValueError):lexical.query_sql('memory_search;DROP TABLE users', ['alpha'], ('u','c'), 1)

    def test_actual_retrieval_facades_dispatch_postgres_without_MATCH(self):
        captured=[]
        database=types.SimpleNamespace(is_postgres=lambda:True,
            query=lambda sql,parameters:(captured.append((sql,parameters))or[{'id':7}]))
        root=Path(__file__).parent
        for filename,name,arguments in [('memory.py','retrieve',('u','c','alpha-beta',3)),
                                        ('document_rag.py','lexical',('u',['f1','f2'],'中文资源',2))]:
            tree=ast.parse((root/filename).read_text(encoding='utf-8'))
            node=next(n for n in tree.body if isinstance(n,ast.FunctionDef)and n.name==name)
            namespace={'db':database,'lexical_search':lexical,'terms':memory_terms,
                       'memory':types.SimpleNamespace(terms=memory_terms)}
            exec(compile(ast.Module([node],type_ignores=[]),'actual-retrieval-facade','exec'),namespace)
            self.assertEqual(namespace[name](*arguments),[{'id':7}])
            sql,parameters=captured[-1]
            self.assertIn('query_phrases',sql)
            self.assertNotIn(' MATCH ',sql)
            self.assertNotIn('bm25(',sql)
            self.assertIn('u',parameters)

    def test_reference_formula_matches_real_FTS5_including_phrases_and_global_rows(self):
        corpus = {1:'alpha-beta 中文', 2:'alpha beta alpha_beta 中文 化学', 3:'alpha alpha alpha beta', 4:'', 5:'unrelated 文学', 6:'beta alpha beta alpha-beta'}
        with sqlite3.connect(':memory:') as conn:
            conn.execute("CREATE VIRTUAL TABLE f USING fts5(terms,tokenize='unicode61')")
            conn.executemany('INSERT INTO f(rowid,terms) VALUES(?,?)', corpus.items())
            for terms in [['alpha'], ['alpha-beta'], ['alpha_beta','alpha-beta'], ['中文','化学'], ['䷀','alpha'], ['absent']]:
                rows = dict(conn.execute('SELECT rowid,bm25(f) FROM f WHERE f MATCH ?', (match_expression(terms),)))
                got = python_scores(corpus, terms)
                self.assertEqual(set(rows), set(got))
                for key in rows:self.assertTrue(math.isclose(rows[key], got[key], rel_tol=2e-13, abs_tol=1e-16), (terms,key,rows[key],got[key]))


DSN = os.environ.get('WUYU_PG_TEST_DSN', '')


@unittest.skipUnless(DSN, 'requires explicit isolated PostgreSQL fixture DSN')
class PostgreSQLDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import psycopg
        from psycopg.conninfo import conninfo_to_dict
        from psycopg.rows import dict_row
        database = conninfo_to_dict(DSN).get('dbname', '')
        if not database.startswith(('wuyu_qa111_fixture','qa111_fixture')):
            raise RuntimeError('Refusing non-fixture PostgreSQL database')
        try:cls.pg = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
        except psycopg.Error:raise RuntimeError('PostgreSQL fixture unavailable') from None
        cls.schema = 'qa111_lex_'+uuid.uuid4().hex
        cls.pg.execute('CREATE SCHEMA '+cls.schema)
        cls.pg.execute('SET search_path TO '+cls.schema)
        for kind, (search, chunks) in lexical.TABLES.items():
            cls.pg.execute(f'CREATE TABLE {search}(rowid BIGINT PRIMARY KEY,terms TEXT NOT NULL,tokens TEXT[] NOT NULL)')
            cls.pg.execute(f'CREATE INDEX {search}_tokens ON {search} USING GIN(tokens)')
            cls.pg.execute(f'CREATE TABLE {chunks}(id BIGINT PRIMARY KEY,user_id TEXT,conversation_id TEXT,file_id TEXT,created DOUBLE PRECISION,text TEXT)')

    @classmethod
    def tearDownClass(cls):
        try:
            cls.pg.execute('SET search_path TO public')
            cls.pg.execute('DROP SCHEMA '+cls.schema+' CASCADE')
        finally:cls.pg.close()

    def setUp(self):
        self.sqlite = sqlite3.connect(':memory:');self.sqlite.row_factory = sqlite3.Row
        self.corpus = {}
        for search, chunks in lexical.TABLES.values():
            self.pg.execute(f'TRUNCATE {search},{chunks}')
            self.sqlite.execute(f"CREATE VIRTUAL TABLE {search} USING fts5(terms,tokenize='unicode61')")
            self.sqlite.execute(f'CREATE TABLE {chunks}(id INTEGER PRIMARY KEY,user_id TEXT,conversation_id TEXT,file_id TEXT,created REAL,text TEXT)')
        outer = self
        class Database:
            dialect = 'postgresql'
            def execute(self, sql, params=()):return outer.pg.execute(sql.replace('?', '%s'), params)
            def query(self, sql, params=()):return self.execute(sql, params).fetchall()
        self.database = Database()

    def tearDown(self):self.sqlite.close()

    def add(self, kind, rowid, terms, uid='owner', cid='chat', fid='file', created=1.0):
        search, chunks = lexical.TABLES[kind]
        item = (rowid,uid,cid,fid,created,'synthetic-'+str(rowid))
        lexical.insert(self.database, search, rowid, terms)
        lexical.insert(self.sqlite, search, rowid, terms)
        self.pg.execute(f'INSERT INTO {chunks} VALUES(%s,%s,%s,%s,%s,%s)', item)
        self.sqlite.execute(f'INSERT INTO {chunks} VALUES(?,?,?,?,?,?)', item)

    def compare(self, kind, query_terms, uid='owner', cid='chat', fids=('file',), limit=40):
        search, chunks = lexical.TABLES[kind]
        if kind=='memory':
            predicate='c.user_id=? AND c.conversation_id=?';scope=(uid,cid);params=(uid,cid);order='rank,c.created DESC,c.id DESC'
        else:
            predicate='c.user_id=? AND c.file_id IN ('+','.join('?'for _ in fids)+')';scope=(uid,list(fids));params=(uid,*fids);order='rank,c.id'
        got = lexical.retrieve(self.database,kind,query_terms,scope,limit)
        oracle = list(self.sqlite.execute(f'SELECT c.*,bm25({search})AS rank FROM {search} JOIN {chunks} c ON c.id={search}.rowid WHERE {search} MATCH ? AND {predicate} ORDER BY {order} LIMIT ?', (match_expression(query_terms),*params,limit))) if query_terms else []
        self.assertEqual([r['id']for r in got],[r['id']for r in oracle])
        for left,right in zip(got,oracle):
            self.assertEqual({k:v for k,v in left.items()if k!='rank'},{k:right[k]for k in right.keys()if k!='rank'})
            self.assertTrue(math.isclose(left['rank'],right['rank'],rel_tol=2e-13,abs_tol=1e-16),(left['rank'],right['rank']))
        return got

    def test_multilingual_phrase_recall_order_and_limits(self):
        source = ['', '固废 资源 循环', '生物炭 固废 资源', 'alpha-beta alpha_beta alpha beta', 'alpha beta alpha', '材料 第五 页码', 'common common rare', 'common rare', '䷀中文 ䷁ 化学', 'abc_def UV-2 中文']
        for kind in lexical.TABLES:
            for rowid,text in enumerate(source,1):self.add(kind,rowid,text,created=float(rowid%3))
            for query in ['固废资源循环','alpha-beta alpha_beta','alpha beta','第五页码','common rare','中文化学','abc_def UV-2','没有此词','䷀']:
                terms=memory_terms(query)
                for limit in [0,1,4,40,-1]:self.compare(kind,terms,limit=limit)

    def test_owner_conversation_and_file_predicates_precede_limit(self):
        for kind in lexical.TABLES:
            self.add(kind,1,'alpha',uid='foreign',cid='chat',fid='foreign')
            self.add(kind,2,'alpha beta',uid='owner',cid='other',fid='other')
            self.add(kind,3,'alpha beta gamma',uid='owner',cid='chat',fid='file')
            got=self.compare(kind,['alpha'],limit=1)
            self.assertEqual([r['id']for r in got],[3])
            self.assertEqual(self.compare(kind,['alpha'],uid="owner' OR true --"),[])

    def test_global_document_frequency_includes_unowned_and_orphan_index_rows(self):
        for kind,(search,chunks) in lexical.TABLES.items():
            self.add(kind,1,'rare alpha beta')
            self.add(kind,2,'alpha beta')
            for i in range(3,15):self.add(kind,i,'alpha beta',uid='foreign',fid='foreign')
            lexical.insert(self.database,search,99,'rare')
            lexical.insert(self.sqlite,search,99,'rare')
            self.compare(kind,['rare','alpha'])

    def test_randomized_synthetic_corpora_and_stable_ties(self):
        rng=random.Random(111)
        words=['alpha','beta','gamma','x_y','x-y','中文','资源','化学','材料','rare','䷀','ab--cd']
        for kind in lexical.TABLES:
            for rowid in range(1,81):
                self.add(kind,rowid,' '.join(rng.choices(words,k=rng.randrange(0,15))),uid='foreign'if rowid%9==0 else'owner',created=float(rowid%4))
            for _ in range(36):self.compare(kind,rng.choices(words,k=rng.randrange(1,7)),limit=rng.choice([1,3,40]))

    def test_bytea_vector_storage_and_signature_contract_are_untouched(self):
        import struct
        packed=struct.pack('<ffff',0.0,-1.0,0.25,1.0)
        self.pg.execute('CREATE TEMP TABLE vector_roundtrip(value BYTEA)')
        self.pg.execute('INSERT INTO vector_roundtrip VALUES(%s)',(packed,))
        self.assertEqual(bytes(self.pg.execute('SELECT value FROM vector_roundtrip').fetchone()['value']),packed)
        self.pg.execute('DROP TABLE vector_roundtrip')
        self.assertEqual(struct.unpack('<ffff',packed),(0.0,-1.0,0.25,1.0))


if __name__=='__main__':unittest.main(verbosity=2)
