import decimal
import unittest
from housing_relief.accounting.settlement_book import SettlementBook
D=decimal.Decimal

class SettlementBookTests(unittest.TestCase):
 def setUp(self):
  self.book=SettlementBook()
  self.policy={'policy_id':'degree','effective_from':'2026-01-01','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
 def claim(self,**changes):
  value={'period':'2026-11','rent':'2400','lease_start':'2026-11-01','lease_end':'2026-11-30','household_income':'5000'}
  value.update(changes);return value
 def test_full_month(self):
  out=self.book.settle(self.claim(),{'policies':[self.policy]});self.assertEqual(out['benefit'],'600.00');self.assertTrue(out['balanced']);self.assertEqual(out['fraction'],'1')
 def test_proration(self):
  out=self.book.settle(self.claim(rent='3000',lease_start='2026-11-16',lease_end='2026-11-30'),{'policies':[self.policy]});self.assertEqual(out['fraction'],'0.5');self.assertEqual(out['benefit'],'300.00')
 def test_income_cap(self):self.assertEqual(self.book.settle(self.claim(household_income='20000'),{'policies':[self.policy]})['benefit'],'0.00')
 def test_exclusive_group_selects_larger(self):self.assertEqual(self.book.settle(self.claim(),{'policies':[self.policy,{**self.policy,'policy_id':'doctor','monthly_cap':'900'}]})['policies'],['doctor'])
 def test_recovery_installments(self):self.assertEqual(self.book.recover([{'amount':'1000','status':'paid'}],decimal.Decimal('250'))['installments'],['300','300','150'])

class MidMonthPolicyTests(unittest.TestCase):
 """Regression: policy effective mid-month must intersect with lease-covered days."""
 def setUp(self):
  self.book=SettlementBook()
  self.policy={'policy_id':'degree','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
 def claim(self,**changes):
  value={'period':'2026-11','rent':'3000','lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000'}
  value.update(changes);return value
 def test_mid_month_policy_grants_prorated_relief(self):
  out=self.book.settle(self.claim(),{'policies':[self.policy]})
  self.assertEqual(out['gross'],'1500.00');self.assertEqual(out['benefit'],'300.00');self.assertEqual(out['payable'],'1200.00')
  self.assertTrue(out['balanced']);self.assertEqual(out['policies'],['degree']);self.assertTrue(out['eligible'])
 def test_relief_only_on_covered_days(self):
  out=self.book.settle(self.claim(),{'policies':[self.policy]})
  self.assertEqual(len(out['segments']),1)
  segment=out['segments'][0]
  self.assertEqual((segment['start'],segment['end']),('2026-11-16','2026-11-30'))
  self.assertEqual(segment['days'],15);self.assertEqual(segment['fraction'],'0.5');self.assertEqual(segment['benefit'],'300.00')
 def test_first_of_month_lease_still_settles(self):
  out=self.book.settle(self.claim(lease_start='2026-11-01',lease_end='2026-11-30'),{'policies':[{**self.policy,'effective_from':'2026-11-01'}]})
  self.assertEqual(out['gross'],'3000.00');self.assertEqual(out['benefit'],'600.00');self.assertEqual(out['payable'],'2400.00')
 def test_future_policy_does_not_apply(self):
  future={**self.policy,'policy_id':'next','effective_from':'2026-12-01'}
  out=self.book.settle(self.claim(),{'policies':[future]})
  self.assertEqual(out['benefit'],'0.00');self.assertEqual(out['policies'],[])
 def test_expired_policy_does_not_apply(self):
  expired={**self.policy,'policy_id':'old','effective_from':'2026-10-01','effective_to':'2026-10-31'}
  out=self.book.settle(self.claim(lease_start='2026-11-01'),{'policies':[expired]})
  self.assertEqual(out['benefit'],'0.00');self.assertEqual(out['policies'],[])
 def test_policy_ending_mid_lease_only_covers_active_days(self):
  ended={**self.policy,'effective_from':'2026-01-01','effective_to':'2026-11-20'}
  out=self.book.settle(self.claim(lease_start='2026-11-01',lease_end='2026-11-30'),{'policies':[ended]})
  self.assertEqual([(s['start'],s['end']) for s in out['segments'] if s['policies']],[('2026-11-01','2026-11-20')])
  # 20 covered days out of 30 -> 600 * 20/30 = 400
  self.assertEqual(out['benefit'],'400.00')

class PolicyVersionChangeTests(unittest.TestCase):
 """Same-month policy replacement must be segmented, never whole-month stale or doubled."""
 def setUp(self):
  self.book=SettlementBook()
  self.v1={'policy_id':'v1','effective_from':'2026-11-15','effective_to':'2026-11-19','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  self.v2={'policy_id':'v2','effective_from':'2026-11-20','monthly_cap':'900','income_cap':'10000','exclusive_group':'housing'}
 def claim(self,**changes):
  value={'period':'2026-11','rent':'3000','lease_start':'2026-11-01','lease_end':'2026-11-30','household_income':'5000'}
  value.update(changes);return value
 def test_each_version_applies_only_to_its_window(self):
  out=self.book.settle(self.claim(),{'policies':[self.v1,self.v2]})
  covered=[s for s in out['segments'] if s['policies']]
  self.assertEqual([(s['start'],s['end'],s['policies']) for s in covered],[('2026-11-15','2026-11-19',['v1']),('2026-11-20','2026-11-30',['v2'])])
  # v1: 5 days -> 600*5/30=100 ; v2: 11 days -> 900*11/30=330
  self.assertEqual(out['benefit'],'430.00');self.assertEqual(out['payable'],'2570.00')
 def test_no_double_relief_and_conservation(self):
  out=self.book.settle(self.claim(),{'policies':[self.v1,self.v2]})
  self.assertLessEqual(D(out['benefit']),D(out['gross']))
  self.assertEqual(sum(D(s['benefit']) for s in out['segments']),D(out['benefit']))
  self.assertEqual(sum(D(s['gross']) for s in out['segments']),D(out['gross']))
  self.assertTrue(out['balanced'])
 def test_expired_version_not_applied_whole_month(self):
  # only v1 exists and it ends 11/19
  out=self.book.settle(self.claim(),{'policies':[self.v1]})
  self.assertEqual(out['policies'],['v1'])
  self.assertTrue(all(s['end']<='2026-11-19' for s in out['segments'] if s['policies']))
  self.assertEqual(out['benefit'],'100.00')

class MutuallyExclusiveTreatmentTests(unittest.TestCase):
 def setUp(self):
  self.book=SettlementBook()
 def test_overlapping_same_group_picks_larger_per_day(self):
  small={'policy_id':'small','effective_from':'2026-11-15','monthly_cap':'100','income_cap':'10000','exclusive_group':'housing'}
  large={'policy_id':'large','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  out=self.book.settle({'period':'2026-11','rent':'3000','lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000'},{'policies':[small,large]})
  self.assertEqual(out['policies'],['large']);self.assertEqual(out['benefit'],'300.00')
 def test_different_groups_stack_without_exceeding_gross(self):
  housing={'policy_id':'housing','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  living={'policy_id':'living','effective_from':'2026-11-15','monthly_cap':'300','income_cap':'10000','exclusive_group':'living'}
  out=self.book.settle({'period':'2026-11','rent':'3000','lease_start':'2026-11-01','lease_end':'2026-11-30','household_income':'5000'},{'policies':[housing,living]})
  self.assertEqual(set(out['policies']),{'housing','living'})
  # both policies effective 11/15 -> 16 covered days; (600+300)*16/30 = 480
  self.assertEqual(out['benefit'],'480.00');self.assertLessEqual(D(out['benefit']),D(out['gross']))
 def test_combined_caps_above_rent_floored_at_gross(self):
  housing={'policy_id':'housing','effective_from':'2026-11-15','monthly_cap':'6000','income_cap':'10000','exclusive_group':'housing'}
  out=self.book.settle({'period':'2026-11','rent':'100','lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000'},{'policies':[housing]})
  self.assertEqual(out['benefit'],'50.00');self.assertEqual(out['payable'],'0.00')

class EligibilityGuardTests(unittest.TestCase):
 def setUp(self):
  self.book=SettlementBook()
  self.policy={'policy_id':'degree','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
 def claim(self,**changes):
  value={'period':'2026-11','rent':'3000','lease_start':'2026-11-16','lease_end':'2026-11-30','household_income':'5000'}
  value.update(changes);return value
 def test_missing_income_fact_never_defaults_to_relief(self):
  claim=self.claim();del claim['household_income']
  out=self.book.settle(claim,{'policies':[self.policy]})
  self.assertFalse(out['eligible']);self.assertEqual(out['benefit'],'0.00')
  self.assertIn('missing_household_income',out['reasons']);self.assertEqual(out['segments'],[])
 def test_invalid_lease_status_never_relieved(self):
  for status in ('cancelled','suspended','draft','completed'):
   out=self.book.settle(self.claim(status=status),{'policies':[self.policy]})
   self.assertFalse(out['eligible'],status);self.assertEqual(out['benefit'],'0.00',status)
   self.assertIn('invalid_lease_status',out['reasons'])
 def test_valid_lease_states_qualify(self):
  for status in ('active','received','validated'):
   out=self.book.settle(self.claim(status=status),{'policies':[self.policy]})
   self.assertTrue(out['eligible'],status);self.assertEqual(out['benefit'],'300.00',status)
 def test_inactive_policy_row_ignored(self):
  inactive={**self.policy,'status':'cancelled'}
  out=self.book.settle(self.claim(),{'policies':[inactive]})
  self.assertEqual(out['benefit'],'0.00');self.assertEqual(out['policies'],[])
 def test_reversed_lease_window_rejected(self):
  out=self.book.settle(self.claim(lease_start='2026-11-30',lease_end='2026-11-01'),{'policies':[self.policy]})
  self.assertFalse(out['eligible']);self.assertIn('invalid_lease_window',out['reasons']);self.assertEqual(out['benefit'],'0.00')

class JournalAndCalendarTests(unittest.TestCase):
 def setUp(self):self.book=SettlementBook()
 def test_february_proration(self):
  policy={'policy_id':'p','effective_from':'2026-02-10','monthly_cap':'280','income_cap':'10000','exclusive_group':'housing'}
  out=self.book.settle({'period':'2026-02','rent':'2800','lease_start':'2026-02-15','lease_end':'2026-02-28','household_income':'5000'},{'policies':[policy]})
  self.assertEqual(out['fraction'],'0.5');self.assertEqual(out['gross'],'1400.00');self.assertEqual(out['benefit'],'140.00');self.assertTrue(out['balanced'])
 def test_cross_month_lease_uses_only_current_month_days(self):
  policy={'policy_id':'p','effective_from':'2026-01-01','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  out=self.book.settle({'period':'2026-11','rent':'3000','lease_start':'2026-11-16','lease_end':'2026-12-15','household_income':'5000'},{'policies':[policy]})
  self.assertEqual(out['fraction'],'0.5');self.assertEqual(out['gross'],'1500.00');self.assertEqual(out['benefit'],'300.00')
 def test_journal_always_balanced_with_rounding(self):
  policy={'policy_id':'p','effective_from':'2026-11-15','monthly_cap':'600','income_cap':'10000','exclusive_group':'housing'}
  for rent in ('1001','333.33','999.99','777.77'):
   out=self.book.settle({'period':'2026-11','rent':rent,'lease_start':'2026-11-01','lease_end':'2026-11-30','household_income':'5000'},{'policies':[policy]})
   self.assertTrue(out['balanced'],rent)
   self.assertEqual(D(out['gross']),D(out['benefit'])+D(out['payable']),rent)
   self.assertEqual(sum(D(s['gross']) for s in out['segments']),D(out['gross']),rent)
