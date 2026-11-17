from __future__ import annotations
import datetime as dt
import decimal
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
D=decimal.Decimal
CENT=D('0.01')
# Lease/record states that represent a valid, enforceable lease.
VALID_LEASE_STATES=frozenset({'active','received','validated'})
# Policy rows without an explicit status are treated as active for backward compatibility.
VALID_POLICY_STATES=frozenset({'active','received','validated',None,''})

@dataclass(frozen=True)
class PolicyVersion:
    policy_id:str; effective_from:dt.date; effective_to:dt.date|None; monthly_cap:D; income_cap:D; exclusive_group:str
    def active(self,day):return self.effective_from<=day and (self.effective_to is None or day<=self.effective_to)

class SettlementBook:
    def policy(self,row):return PolicyVersion(str(row['policy_id']),dt.date.fromisoformat(str(row['effective_from'])),dt.date.fromisoformat(str(row['effective_to'])) if row.get('effective_to') else None,D(str(row.get('monthly_cap','0'))),D(str(row.get('income_cap','999999999'))),str(row.get('exclusive_group','default')))

    # ---------- date helpers ----------
    def _month_bounds(self,month:dt.date):
        first=month.replace(day=1)
        next_month=(month.replace(day=28)+dt.timedelta(days=4)).replace(day=1)
        last=next_month-dt.timedelta(days=1)
        return first,last

    def _overlap(self,left:dt.date,right:dt.date,start:dt.date,end:dt.date|None):
        lo=max(left,start)
        hi=min(right,end) if end is not None else right
        if hi<lo:return 0
        return (hi-lo).days+1

    def month_fraction(self,start,end,month):
        first,last=self._month_bounds(month)
        left=max(start,first);right=min(end,last)
        return D(max(0,(right-left).days+1))/D(last.day)

    # ---------- eligibility ----------
    def _lease_window(self,claim,first:dt.date,last:dt.date):
        lease_start=dt.date.fromisoformat(str(claim.get('lease_start',first)))
        lease_end=dt.date.fromisoformat(str(claim.get('lease_end',last)))
        return lease_start,lease_end

    def _lease_status_ok(self,claim):
        # An explicitly invalid lease never qualifies. Missing status is treated as
        # valid for backward compatibility with historical claim payloads.
        status=claim.get('status')
        if status is None or status=='':return True
        return str(status) in VALID_LEASE_STATES

    def _income_ok(self,claim,policy:PolicyVersion):
        raw=claim.get('household_income')
        if raw is None or raw=='':
            return False  # missing qualification fact must not default to relief
        return D(str(raw))<=policy.income_cap

    def _candidate_policies(self,claim,policies,first:dt.date,last:dt.date,lease_start:dt.date,lease_end:dt.date):
        candidates=[]
        for row in policies:
            if row.get('status','active') not in VALID_POLICY_STATES:continue
            policy=self.policy(row)
            if not self._income_ok(claim,policy):continue
            # intersect the three closed intervals: month, lease window, policy window
            policy_hi=policy.effective_to if policy.effective_to is not None else last
            lo=max(first,lease_start,policy.effective_from)
            hi=min(last,lease_end,policy_hi)
            if lo>hi:continue
            candidates.append(policy)
        return candidates

    def select_policies(self,claim,policies):
        # Kept for backward compatibility: policies that govern the claim at any
        # covered day of the month (not only on day one).
        first,last=self._month_bounds(dt.date.fromisoformat(str(claim['period'])+'-01'))
        lease_start,lease_end=self._lease_window(claim,first,last)
        active=self._candidate_policies(claim,policies,first,last,lease_start,lease_end)
        by_group={}
        for policy in active:
            old=by_group.get(policy.exclusive_group)
            if old is None or policy.monthly_cap>old.monthly_cap:by_group[policy.exclusive_group]=policy
        return sorted(by_group.values(),key=lambda policy:policy.policy_id)

    # ---------- daily segmentation ----------
    def _segments(self,first:dt.date,last:dt.date,lease_start:dt.date,lease_end:dt.date,policies:Sequence[PolicyVersion]):
        # Collect every boundary where the set of governing policies can change.
        boundaries={first,last+dt.timedelta(days=1),max(first,lease_start)}
        if lease_end is not None and lease_end<=last:
            boundaries.add(lease_end+dt.timedelta(days=1))
        for policy in policies:
            boundaries.add(max(first,policy.effective_from))
            if policy.effective_to is not None:
                boundaries.add(max(first,policy.effective_to+dt.timedelta(days=1)))
        points=sorted(d for d in boundaries if first<=d<=last+dt.timedelta(days=1))
        segments=[]
        for index in range(len(points)-1):
            seg_start=points[index];seg_end=points[index+1]-dt.timedelta(days=1)
            if seg_end<seg_start:continue
            covered=self._overlap(seg_start,seg_end,lease_start,lease_end)
            if covered<=0:continue
            day=seg_start  # all policies are constant within a segment
            winners={}
            for policy in policies:
                if not policy.active(day):continue
                old=winners.get(policy.exclusive_group)
                # mutually exclusive treatment: one policy per group per day;
                # larger monthly cap wins, ties resolved by policy_id for stability
                if old is None or policy.monthly_cap>old.monthly_cap or (policy.monthly_cap==old.monthly_cap and policy.policy_id<old.policy_id):
                    winners[policy.exclusive_group]=policy
            chosen=sorted(winners.values(),key=lambda item:item.policy_id)
            segments.append({'start':seg_start,'end':seg_end,'days':covered,'policies':tuple(chosen)})
        return segments

    def settle(self,claim,context):
        period=dt.date.fromisoformat(str(claim['period'])+'-01')
        first,last=self._month_bounds(period)
        lease_start,lease_end=self._lease_window(claim,first,last)
        rent=D(str(claim.get('rent',claim.get('amount','0'))))
        reasons=[]
        if lease_end<lease_start:reasons.append('invalid_lease_window')
        if not self._lease_status_ok(claim):reasons.append('invalid_lease_status')
        if claim.get('household_income') in {None,''}:reasons.append('missing_household_income')
        policies=self._candidate_policies(claim,context.get('policies',()),first,last,lease_start,lease_end)
        fraction=self.month_fraction(lease_start,lease_end,period)
        gross=(rent*fraction).quantize(CENT)

        segments=[]
        if not reasons:
            segments=self._segments(first,last,lease_start,lease_end,policies)
        benefit_total=D('0.00');gross_total=D('0.00')
        segment_rows=[]
        for segment in segments:
            seg_fraction=D(segment['days'])/D(last.day)
            seg_gross=(rent*seg_fraction).quantize(CENT)
            seg_cap=sum((item.monthly_cap*seg_fraction for item in segment['policies']),D('0'))
            seg_benefit=min(seg_gross,seg_cap).quantize(CENT)
            benefit_total+=seg_benefit;gross_total+=seg_gross
            segment_rows.append({
                'start':segment['start'].isoformat(),'end':segment['end'].isoformat(),
                'days':segment['days'],'fraction':str(seg_fraction),
                'gross':str(seg_gross),'benefit':str(seg_benefit),
                'policies':[item.policy_id for item in segment['policies']],
                'groups':sorted({item.exclusive_group for item in segment['policies']}),
            })
        # Segments cover exactly the lease-covered days; the quantized segment gross
        # totals must agree with the single-proration gross so the journal balances.
        benefit=benefit_total.quantize(CENT)
        if gross_total!=gross and segment_rows:
            # absorb rounding drift on the last segment instead of breaking the journal
            drift=gross-gross_total
            row=dict(segment_rows[-1]);row['gross']=str(D(row['gross'])+drift)
            segment_rows[-1]=row
        benefit=min(benefit,gross)
        payable=(gross-benefit).quantize(CENT)
        entries=[('rent_receivable',gross),('policy_benefit',-benefit),('tenant_cash',-payable)]
        selected=sorted({policy_id for row in segment_rows for policy_id in row['policies']})
        return {
            'gross':str(gross),'benefit':str(benefit),'payable':str(payable),
            'balanced':sum(v for _,v in entries)==0,
            'entries':[(n,str(v)) for n,v in entries],
            'policies':selected,'fraction':str(fraction),
            'eligible':not reasons,'reasons':tuple(reasons),
            'lease_start':lease_start.isoformat(),'lease_end':lease_end.isoformat(),
            'segments':segment_rows,
        }

    def recover(self,paid,entitled):
        total=sum((D(str(row['amount'])) for row in paid if row.get('status')!='reversed'),D('0'));over=max(D('0'),total-entitled);installments=[];remaining=over
        while remaining>0:part=min(D('300'),remaining);installments.append(str(part));remaining-=part
        return {'paid':str(total),'entitled':str(entitled),'overpayment':str(over),'installments':installments}
