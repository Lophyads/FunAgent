// Optional, fully local PostgreSQL/pgvector contract test (no Supabase credentials).
// npm install --prefix .funagent/sql-test-runtime --no-audit --no-fund @electric-sql/pglite @electric-sql/pglite-pgvector
// node --test tests/supabase_sql.mjs
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {resolve} from 'node:path';
import {after, test} from 'node:test';

const require = createRequire(resolve(process.env.FUNAGENT_SQL_TEST_RUNTIME || '.funagent/sql-test-runtime', 'package.json'));
const {PGlite} = require('@electric-sql/pglite');
const {vector} = require('@electric-sql/pglite-pgvector');
const db = new PGlite({extensions:{vector}});
after(() => db.close());
await db.exec('create role anon; create role authenticated; create role service_role bypassrls;');
const sql = await readFile(new URL('../sql/supabase/001_memory.sql', import.meta.url), 'utf8');
await db.exec(sql);
await db.exec(sql); // Deployment can be repeated without dropping records/schema.
await db.exec('set role service_role;');

const vec = (index=0) => Array.from({length:1024}, (_, i) => i === index ? 1 : 0);
const draft = (id, overrides={}) => ({id, kind:'user', subject:'Tea', content:'Prefers tea',
  source:{session_id:'s', turn_id:'t'}, event_at:null, dedupe_key:id, embedding:vec(), ...overrides});
async function write(ns, action, data, profile='model-v1') {
  return (await db.query('select public.funagent_memory_write($1,$2,$3,$4::jsonb) result',
    [ns, profile, action, JSON.stringify(data)])).rows[0].result;
}
async function list(ns, kind=null, limit=20, after=null, id=null, profile='model-v1') {
  return (await db.query('select public.funagent_memory_list($1,$2,$3,$4,$5::jsonb,$6) result',
    [ns, profile, kind, limit, after && JSON.stringify(after), id])).rows[0].result;
}
async function search(ns, embedding=vec(), kind=null, threshold=0.5, profile='model-v1') {
  return (await db.query('select public.funagent_memory_search($1,$2,$3,$4,$5::double precision[],$6) result',
    [ns, profile, kind, 20, embedding, threshold])).rows[0].result;
}

test('schema, CRUD, version conflict, duplicate, and hidden vector fields', async () => {
  const schema = (await db.query('select public.funagent_memory_schema($1,$2) result', ['crud','model-v1'])).rows[0].result;
  assert.deepEqual(schema, {status:'ok', version:1, dimensions:1024});
  const created = await write('crud','add',draft('opaque:one'));
  assert.equal(created.status, 'ok');
  assert.equal(created.entry.version, 1);
  assert.equal(created.entry.id, 'opaque:one');
  assert.ok(!('embedding' in created.entry) && !('dedupe_key' in created.entry));
  assert.equal((await write('crud','add',draft('opaque:two',{dedupe_key:'opaque:one'}))).status,'duplicate');
  const changed = await write('crud','update',draft('opaque:one',{content:'Prefers coffee',expected_version:1,embedding:vec(1)}));
  assert.equal(changed.entry.version, 2);
  assert.equal(changed.entry.created_at, created.entry.created_at);
  assert.deepEqual(changed.entry.source, created.entry.source);
  assert.equal((await write('crud','update',draft('opaque:one',{content:'stale',expected_version:1}))).status,'conflict');
  assert.equal((await write('crud','delete',{id:'opaque:one',expected_version:1})).status,'conflict');
  assert.equal((await write('crud','delete',{id:'opaque:one',expected_version:2})).status,'ok');
  assert.equal((await write('crud','delete',{id:'opaque:one',expected_version:2})).status,'missing');
  assert.equal((await list('crud')).total, 0);
});

test('namespace/kind filters, semantic threshold, update and delete visibility', async () => {
  await write('search','add',draft('tea'));
  await write('search','add',draft('coffee',{kind:'note',embedding:vec(1)}));
  await write('other','add',draft('secret'));
  assert.deepEqual((await search('search')).entries.map(e=>e.id), ['tea']);
  assert.deepEqual((await search('search',vec(),'note')).entries, []);
  assert.deepEqual((await search('search',vec(2))).entries, []);
  await write('search','update',draft('tea',{expected_version:1,embedding:vec(1)}));
  assert.deepEqual((await search('search')).entries, []);
  assert.equal((await search('search',vec(1))).entries.length, 2);
  await write('search','delete',{id:'tea',expected_version:2});
  assert.deepEqual((await search('search',vec(1))).entries.map(e=>e.id), ['coffee']);
});

test('keyset paging including tied timestamps and kind totals', async () => {
  for (let i=0;i<5;i++) await write('paging','add',draft(`p${i}`,{kind:i%2 ? 'note' : 'user'}));
  await db.query("update public.funagent_memory_entries set updated_at='2026-10-05T10:00:00.123456Z' where namespace=$1",['paging']);
  const first = await list('paging',null,2);
  assert.equal(first.total,5);
  assert.deepEqual(first.entries.map(e=>e.id),['p4','p3','p2']);
  const last = first.entries[1];
  const second = await list('paging',null,2,{updated_at:last.updated_at,id:last.id});
  assert.deepEqual(second.entries.map(e=>e.id),['p2','p1','p0']);
  assert.equal((await list('paging','note')).total,2);
});

test('episode replay keeps original dedupe key after editing', async () => {
  const original = draft('event',{kind:'episode',event_at:'2026-10-05',dedupe_key:'original-range'});
  await write('episodes','add',original);
  const edited = await write('episodes','update',{...original,content:'Changed outcome',event_at:null,dedupe_key:'changed',expected_version:1});
  assert.equal(edited.entry.event_at,null);
  const replay = await write('episodes','add',{...original,id:'replayed'});
  assert.equal(replay.status,'duplicate');
  assert.equal(replay.entry.content,'Changed outcome');
});

test('profile changes and malformed vectors never overwrite records', async () => {
  await write('guard','add',draft('original'));
  assert.equal((await write('guard','add',draft('new'),'model-v2')).status,'profile_mismatch');
  assert.equal((await search('guard',vec(),null,0.5,'model-v2')).status,'profile_mismatch');
  assert.equal((await list('guard',null,20,null,null,'model-v2')).status,'profile_mismatch');
  await assert.rejects(write('guard','update',draft('original',{expected_version:1,embedding:[1,0]})));
  assert.equal((await list('guard')).entries[0].version,1);
  assert.equal((await search('guard',[1,0])).status,'invalid');
  assert.equal((await search('guard',Array(1024).fill(0))).status,'invalid');
  assert.equal((await list('guard',null,0)).status,'invalid');
});

test('public/anon/authenticated cannot read tables or execute RPCs', async () => {
  const denied = await db.query(`select
    has_table_privilege('anon','public.funagent_memory_entries','SELECT') as table_read,
    has_function_privilege('authenticated','public.funagent_memory_write(text,text,text,jsonb)','EXECUTE') as write_rpc,
    has_function_privilege('anon','public.funagent_memory_search(text,text,text,integer,double precision[],double precision)','EXECUTE') as search_rpc`);
  assert.deepEqual(denied.rows[0],{table_read:false,write_rpc:false,search_rpc:false});
});
