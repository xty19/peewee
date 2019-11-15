import datetime
import uuid

from peewee import *
from playhouse.cockroach import *

from .base import IS_CRDB
from .base import ModelTestCase
from .base import TestModel
from .base import db
from .base import requires_models
from .base import skip_unless
from .postgres_helpers import BaseBinaryJsonFieldTestCase


class KV(TestModel):
    k = TextField(unique=True)
    v = IntegerField()

class Arr(TestModel):
    title = TextField()
    tags = ArrayField(TextField, index=False)

class JsonModel(TestModel):
    data = JSONField()

class Normal(TestModel):
    data = TextField()

class UID(TestModel):
    id = UUIDKeyField()
    title = TextField()

class RID(TestModel):
    id = RowIDField()
    title = TextField()

class UIDNote(TestModel):
    uid = ForeignKeyField(UID, backref='notes')
    note = TextField()


@skip_unless(IS_CRDB)
class TestCockroachDatabase(ModelTestCase):
    @requires_models(KV)
    def test_retry_transaction_ok(self):
        @self.database.retry_transaction()
        def succeeds(db):
            k1 = KV.create(k='k1', v=1)
            k2 = KV.create(k='k2', v=2)
            return [k1.id, k2.id]

        id_list = succeeds()
        self.assertEqual(KV.select().count(), 2)

        kv_list = [kv.id for kv in KV.select().order_by(KV.k)]
        self.assertEqual(kv_list, id_list)

    @requires_models(KV)
    def test_retry_transaction_integrityerror(self):
        KV.create(k='kx', v=0)

        @self.database.retry_transaction()
        def fails(db):
            KV.create(k='k1', v=1)
            KV.create(k='kx', v=1)

        with self.assertRaises(IntegrityError):
            fails()

        self.assertEqual(KV.select().count(), 1)
        kv = KV.get(KV.k == 'kx')
        self.assertEqual(kv.v, 0)

    @requires_models(KV)
    def test_run_transaction_helper(self):
        def succeeds(db):
            KV.insert_many([('k%s' % i, i) for i in range(10)]).execute()
        run_transaction(self.database, succeeds)
        self.assertEqual([(kv.k, kv.v) for kv in KV.select().order_by(KV.k)],
                         [('k%s' % i, i) for i in range(10)])

    @requires_models(Arr)
    def test_array_field(self):
        a1 = Arr.create(title='a1', tags=['t1', 't2'])
        a2 = Arr.create(title='a2', tags=['t2', 't3'])

        # Ensure we can read an array back.
        a1_db = Arr.get(Arr.title == 'a1')
        self.assertEqual(a1_db.tags, ['t1', 't2'])

        # Ensure we can filter on arrays.
        a2_db = Arr.get(Arr.tags == ['t2', 't3'])
        self.assertEqual(a2_db.id, a2.id)

        # Item lookups.
        a1_db = Arr.get(Arr.tags[1] == 't2')
        self.assertEqual(a1_db.id, a1.id)
        self.assertRaises(Arr.DoesNotExist, Arr.get, Arr.tags[2] == 'x')

    @requires_models(Arr)
    def test_array_field_search(self):
        def assertAM(where, id_list):
            query = Arr.select().where(where).order_by(Arr.title)
            self.assertEqual([a.id for a in query], id_list)

        data = (
            ('a1', ['t1', 't2']),
            ('a2', ['t2', 't3']),
            ('a3', ['t3', 't4']))
        id_list = Arr.insert_many(data).execute()
        a1, a2, a3 = [pk for pk, in id_list]

        assertAM(Value('t2') == fn.ANY(Arr.tags), [a1, a2])
        assertAM(Value('t1') == fn.Any(Arr.tags), [a1])
        assertAM(Value('tx') == fn.Any(Arr.tags), [])

        # Use the contains operator explicitly.
        assertAM(SQL("tags::text[] @> ARRAY['t2']"), [a1, a2])

        # Use the porcelain.
        assertAM(Arr.tags.contains('t2'), [a1, a2])
        assertAM(Arr.tags.contains('t3'), [a2, a3])
        assertAM(Arr.tags.contains('t1', 't2'), [a1])
        assertAM(Arr.tags.contains('t3', 't4'), [a3])
        assertAM(Arr.tags.contains('t2', 't3', 't4'), [])

        assertAM(Arr.tags.contains_any('t2'), [a1, a2])
        assertAM(Arr.tags.contains_any('t3'), [a2, a3])
        assertAM(Arr.tags.contains_any('t1', 't2'), [a1, a2])
        assertAM(Arr.tags.contains_any('t3', 't4'), [a2, a3])
        assertAM(Arr.tags.contains_any('t2', 't3', 't4'), [a1, a2, a3])

    @requires_models(Arr)
    def test_array_field_index(self):
        a1 = Arr.create(title='a1', tags=['a1', 'a2'])
        a2 = Arr.create(title='a2', tags=['a2', 'a3', 'a4', 'a5'])

        # NOTE: CRDB does not support array slicing.
        query = (Arr
                 .select(Arr.tags[1].alias('st'))
                 .order_by(Arr.title))
        self.assertEqual([a.st for a in query], ['a2', 'a3'])

    @requires_models(UID)
    def test_uuid_key_field(self):
        # UUID primary-key is automatically populated and returned, and is of
        # the correct type.
        u1 = UID.create(title='u1')
        self.assertTrue(u1.id is not None)
        self.assertTrue(isinstance(u1.id, uuid.UUID))

        # Bulk-insert works as expected.
        id_list = UID.insert_many([('u2',), ('u3',)]).execute()
        u2_id, u3_id = [pk for pk, in id_list]
        self.assertTrue(isinstance(u2_id, uuid.UUID))

        # We can perform lookups using UUID() type.
        u2 = UID.get(UID.id == u2_id)
        self.assertEqual(u2.title, 'u2')

        # Get the UUID hex and query using that.
        u3 = UID.get(UID.id == u3_id.hex)
        self.assertEqual(u3.title, 'u3')

    @requires_models(RID)
    def test_rowid_field(self):
        r1 = RID.create(title='r1')
        self.assertTrue(r1.id is not None)

        # Bulk-insert works as expected.
        id_list = RID.insert_many([('r2',), ('r3',)]).execute()
        r2_id, r3_id = [pk for pk, in id_list]

        r2 = RID.get(RID.id == r2_id)
        self.assertEqual(r2.title, 'r2')

    @requires_models(KV)
    def test_readonly_transaction(self):
        kv = KV.create(k='k1', v=1)

        # Table doesn't exist yet.
        with self.assertRaises(ProgrammingError):
            with self.database.atomic('-10s'):
                kv_db = KV.get(KV.k == 'k1')

        # Cannot write in a read-only transaction
        with self.assertRaises(ProgrammingError):
            with self.database.atomic(datetime.datetime.now()):
                KV.create(k='k2', v=2)

        # Without system time there are no issues.
        with self.database.atomic():
            kv_db = KV.get(KV.k == 'k1')
            self.assertEqual(kv.id, kv_db.id)

    @requires_models(KV)
    def test_transaction_priority(self):
        with self.database.atomic(priority='HIGH'):
            KV.create(k='k1', v=1)
        with self.assertRaises(IntegrityError):
            with self.database.atomic(priority='LOW'):
                KV.create(k='k1', v=2)
        with self.assertRaises(ValueError):
            with self.database.atomic(priority='HUH'):
                KV.create(k='k2', v=2)

        self.assertEqual(KV.select().count(), 1)
        kv = KV.get()
        self.assertEqual((kv.k, kv.v), ('k1', 1))

    @requires_models(UID, UIDNote)
    def test_uuid_key_as_fk(self):
        # This is covered thoroughly elsewhere, but added here just for fun.
        u1, u2, u3 = [UID.create(title='u%s' % i) for i in (1, 2, 3)]
        UIDNote.create(uid=u1, note='u1-1')
        UIDNote.create(uid=u2, note='u2-1')
        UIDNote.create(uid=u2, note='u2-2')

        with self.assertQueryCount(1):
            query = (UIDNote
                     .select(UIDNote, UID)
                     .join(UID)
                     .where(UID.title == 'u2')
                     .order_by(UIDNote.note))
            self.assertEqual([(un.note, un.uid.title) for un in query],
                             [('u2-1', 'u2'), ('u2-2', 'u2')])

        query = (UID
                 .select(UID, fn.COUNT(UIDNote.id).alias('note_count'))
                 .join(UIDNote, JOIN.LEFT_OUTER)
                 .group_by(UID)
                 .order_by(fn.COUNT(UIDNote.id).desc()))
        self.assertEqual([(u.title, u.note_count) for u in query],
                         [('u2', 2), ('u1', 1), ('u3', 0)])


@skip_unless(IS_CRDB)
class TestCockroachDatabaseJson(BaseBinaryJsonFieldTestCase, ModelTestCase):
    database = db
    M = JsonModel
    N = Normal
    requires = [JsonModel, Normal]
