"""PostgreSQL lexical index with the existing single-column FTS5 semantics.

Only the already-filtered output of memory.terms is accepted. This restricted
ASCII/CJK domain can reproduce unicode61 token boundaries without a SQLite
runtime dependency. SQL calculates phrase hits/global corpus statistics and
uses the same BM25 arithmetic, phrase order and ascending-rank convention.
"""
import re

_INPUT = re.compile(r'[a-zA-Z0-9_\-\s\u3400-\u9fff]*\Z')
_TOKEN = re.compile(r'[a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+')
TABLES = {'memory': ('memory_search', 'memory_chunks'),
          'document': ('document_search', 'document_chunks')}


def tokenize_terms(text):
    """Tokenize stored pre-segmented terms, not arbitrary user prose.

    Hyphens/underscores are unicode61 separators. The only non-token interval
    inside memory.terms' CJK range is U+4DC0..U+4DFF (Yijing symbols). The full
    U+3400..U+9FFF range is exhaustively compared with FTS5 in offline tests.
    """
    if not isinstance(text, str) or not _INPUT.fullmatch(text):
        raise ValueError('Unsupported pre-segmented lexical index terms')
    return _TOKEN.findall(text.lower())


def insert(connection, table, rowid, terms):
    if table not in {value[0] for value in TABLES.values()}:
        raise ValueError('Unknown lexical search table')
    if getattr(connection, 'dialect', 'sqlite') == 'postgresql':
        return connection.execute(f'INSERT INTO {table}(rowid,terms,tokens) VALUES(?,?,?)',
                                  (rowid, terms, tokenize_terms(terms)))
    return connection.execute(f'INSERT INTO {table}(rowid,terms) VALUES(?,?)', (rowid, terms))


def query_sql(kind, query_terms, scope, limit):
    """Return one parameterized query; no text/IDs are interpolated in SQL.

    Corpus count/length and document frequencies remain global, exactly as
    FTS5 bm25(table). Permission predicates filter candidate chunks before
    ranking/LIMIT; no other owner's text or identifiers leave PostgreSQL.
    """
    if kind not in TABLES:
        raise ValueError('Unknown lexical search kind')
    if type(limit) is not int:
        raise ValueError('Invalid lexical retrieval limit')
    phrases = [tokenize_terms(term) for term in query_terms]
    phrases = [phrase for phrase in phrases if phrase]
    if not phrases or limit == 0:
        return None, ()
    if len(phrases) > 48:
        raise ValueError('Lexical query exceeds phrase budget')
    search, chunks = TABLES[kind]
    values = ','.join('(?,?::text[])' for _ in phrases)
    parameters = []
    # Do not deduplicate normalized phrases: distinct input terms such as
    # alpha-beta and alpha_beta are distinct FTS query phrases and both score.
    for ordinal, phrase in enumerate(phrases):
        parameters.extend((ordinal, phrase))
    if kind == 'memory':
        uid, cid = scope
        predicate = 'c.user_id=? AND c.conversation_id=?'
        parameters.extend((uid, cid))
        order = 'r.rank,c.created DESC,c.id DESC'
    else:
        uid, file_ids = scope
        if not file_ids:
            return None, ()
        predicate = 'c.user_id=? AND c.file_id IN (' + ','.join('?' for _ in file_ids) + ')'
        parameters.extend((uid, *file_ids))
        order = 'r.rank,c.id'
    # A negative SQLite LIMIT means unbounded. Preserve this internal contract
    # rather than changing it into PostgreSQL's invalid negative LIMIT.
    limit_sql = '' if limit < 0 else ' LIMIT ?'
    if limit >= 0:
        parameters.append(limit)
    sql = f'''WITH query_phrases(ordinal,phrase) AS (VALUES {values}),
      corpus AS (
        SELECT COUNT(*)::double precision AS n,
          COALESCE(SUM(cardinality(tokens)),0)::double precision /
          NULLIF(COUNT(*),0)::double precision AS avgdl
        FROM {search}
      ),
      phrase_hits AS MATERIALIZED (
        SELECT d.rowid,q.ordinal,cardinality(d.tokens)::double precision AS doclen,
          counts.frequency::double precision AS frequency
        FROM query_phrases q JOIN {search} d ON d.tokens @> q.phrase
        CROSS JOIN LATERAL (
          SELECT COUNT(*) AS frequency FROM generate_subscripts(d.tokens,1) AS positions(i)
          WHERE d.tokens[positions.i:positions.i+cardinality(q.phrase)-1]=q.phrase
        ) counts
        WHERE counts.frequency>0
      ),
      phrase_statistics AS (
        SELECT ordinal,COUNT(*)::double precision AS documents
        FROM phrase_hits GROUP BY ordinal
      ),
      contributions AS (
        SELECT h.rowid,h.ordinal,
          CASE WHEN ln((a.n-f.documents+0.5::double precision)/
                       (f.documents+0.5::double precision))<=0
            THEN 0.000001::double precision
            ELSE ln((a.n-f.documents+0.5::double precision)/
                    (f.documents+0.5::double precision)) END *
          ((h.frequency*(1.2::double precision+1.0::double precision))/
           (h.frequency+1.2::double precision*(1.0::double precision-0.75::double precision+
             0.75::double precision*h.doclen/a.avgdl))) AS value
        FROM phrase_hits h JOIN {chunks} c ON c.id=h.rowid
        JOIN phrase_statistics f ON f.ordinal=h.ordinal CROSS JOIN corpus a
        WHERE {predicate}
      ),
      ranks AS (
        SELECT rowid,-SUM(value ORDER BY ordinal) AS rank
        FROM contributions GROUP BY rowid
      )
      SELECT c.*,r.rank FROM ranks r JOIN {chunks} c ON c.id=r.rowid
      ORDER BY {order}{limit_sql}'''
    return sql, tuple(parameters)


def retrieve(database, kind, query_terms, scope, limit):
    sql, parameters = query_sql(kind, query_terms, scope, limit)
    return database.query(sql, parameters) if sql is not None else []
