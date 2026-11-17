import json
import tempfile
import unittest
from pathlib import Path

from housing_relief.accounting.settlement_book import SettlementBook
from housing_relief.reporting import Reporter
from housing_relief.service import ApplicationService
from housing_relief.storage import Database
from housing_relief.settlement.engine import SettlementEngine

class MidMonthSettlementEndToEndTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory()
  self.db=Database(Path(self.temp.name)/'e2e.sqlite3')
  self.service=ApplicationService(self.db)
  # Relief policy effective 11/15: the exact mid-month policy from the defect report.
  self.context={'policies':[
   {'policy_id':'degree','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  ]}
 def tearDown(self):self.temp.cleanup()

 def _item(self,identifier,**changes):
  value={'id':identifier,'owner_id':'person-1','rent':'3000','period':'2026-11',
         'lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000',
         'effective_from':'2026-11-16','request_key':identifier+'-key','amount':'1500'}
  value.update(changes);return value

 def test_mid_month_lease_bill_is_corrected(self):
  response=self.service.process(self._item('claim-mid'),self.context,'claim-mid-key')
  facts=response['facts']
  self.assertTrue(response['accepted'])
  self.assertEqual(facts['benefit'],'300.00')      # 600 cap * 15/30 days
  self.assertEqual(facts['gross'],'1500.00')
  self.assertEqual(facts['payable'],'1200.00')     # tenant is no longer over-charged
  self.assertTrue(facts['balanced'])
  self.assertEqual(facts['policies'],['degree'])

 def test_personal_bill_persisted_and_visible_in_summary(self):
  self.service.process(self._item('claim-mid'),self.context,'claim-mid-key')
  stored=self.db.get_record('person','claim-mid')
  self.assertEqual(stored['status'],'active')
  self.assertEqual(stored['attributes']['facts']['payable'],'1200.00')
  overview=Reporter(self.db).overview()
  self.assertEqual(overview['total_records'],1)
  breakdown=Reporter(self.db).owner_breakdown('person')
  self.assertEqual(breakdown['person-1']['count'],1)
  # outbox carries the corrected bill as the payment basis
  pending=self.db.pending_messages()
  self.assertEqual(len(pending),1)
  self.assertEqual(pending[0]['payload']['facts']['benefit'],'300.00')

 def test_duplicate_request_is_idempotent(self):
  first=self.service.process(self._item('claim-dup'),self.context,'claim-dup-key')
  second=self.service.process(self._item('claim-dup'),self.context,'claim-dup-key')
  # storage round-trips tuples as lists; compare on canonical JSON
  self.assertEqual(json.dumps(first,sort_keys=True,default=str),json.dumps(second,sort_keys=True,default=str))
  # no duplicate bill, event or outbox message on re-processing
  self.assertEqual(len(self.db.list_records('person')),1)
  self.assertEqual(len(self.db.pending_messages()),1)
  self.assertEqual(len(self.db.events_for('person','claim-dup')),1)

 def test_duplicate_request_with_changed_payload_rejected(self):
  self.service.process(self._item('claim-x'),self.context,'claim-x-key')
  changed=self._item('claim-x',rent='9999')
  from housing_relief.storage import ConflictError
  with self.assertRaises(ConflictError):
   self.service.process(changed,self.context,'claim-x-key')

 def test_invalid_lease_and_missing_facts_produce_no_relief_bill(self):
  invalid=self._item('claim-invalid',status='cancelled',request_key='claim-invalid-key')
  response=self.service.process(invalid,self.context,'claim-invalid-key')
  self.assertFalse(response['accepted'])
  self.assertIn('invalid_lease_status',response['reasons'])
  self.assertEqual(response['facts']['benefit'],'0.00')
  missing=self._item('claim-missing',id='claim-missing',request_key='claim-missing-key')
  del missing['household_income']
  response_missing=self.service.process(missing,self.context,'claim-missing-key')
  self.assertFalse(response_missing['accepted'])
  self.assertIn('missing_household_income',response_missing['reasons'])
  self.assertEqual(response_missing['facts']['benefit'],'0.00')

 def test_existing_valid_bills_and_recovery_installments_preserved(self):
  import decimal
  # a corrected mid-month bill is still a valid, accepted settlement
  engine=SettlementEngine()
  result=engine.evaluate(self._item('claim-keep'),self.context)
  self.assertTrue(result.accepted)
  self.assertEqual(result.facts['payable'],'1200.00')
  # recovery installment behavior for an over-paid bill is unchanged
  plan=SettlementBook().recover([{'amount':'1000','status':'paid'}],decimal.Decimal('250'))
  self.assertEqual(plan['installments'],['300','300','150'])

 def test_version_change_bill_explains_each_segment(self):
  context={'policies':[
   {'policy_id':'v1','effective_from':'2026-11-15','effective_to':'2026-11-19','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'},
   {'policy_id':'v2','effective_from':'2026-11-20','monthly_cap':'900','income_cap':'10000','exclusive_group':'housing'},
  ]}
  item=self._item('claim-ver',lease_start='2026-11-01',lease_end='2026-11-30',amount='3000',request_key='claim-ver-key')
  response=self.service.process(item,context,'claim-ver-key')
  segments=response['facts']['segments']
  covered=[s for s in segments if s['policies']]
  self.assertEqual([(s['start'],s['end'],s['policies'],s['benefit']) for s in covered],
                   [('2026-11-15','2026-11-19',['v1'],'100.00'),('2026-11-20','2026-11-30',['v2'],'330.00')])
  self.assertEqual(response['facts']['benefit'],'430.00')
