use near_sdk::borsh::{self, BorshDeserialize, BorshSerialize};
use near_sdk::collections::{LookupMap, UnorderedMap};
use near_sdk::json_types::U128;
use near_sdk::{env, ext_contract, near_bindgen, AccountId, Gas, PanicOnDefault, Promise, PromiseResult, NearToken};
use near_sdk::serde::{Deserialize, Serialize};

type Balance = u128;

const NO_DEPOSIT: Balance = 0;
const CALLBACK_GAS: Gas = Gas::from_tgas(20);
const REFUND_GAS: Gas = Gas::from_tgas(10);
const NANOS_PER_MONTH: u64 = 30 * 24 * 60 * 60 * 1_000_000_000;
const NANOS_PER_YEAR: u64 = 365 * 24 * 60 * 60 * 1_000_000_000;

#[derive(BorshSerialize, BorshDeserialize, Serialize, Deserialize, Clone, PartialEq, Eq)]
#[serde(crate = "near_sdk::serde")]
pub enum Plan { Monthly, Yearly }

impl Plan {
    fn duration_ns(&self) -> u64 { match self { Self::Monthly => NANOS_PER_MONTH, Self::Yearly => NANOS_PER_YEAR } }
}

#[derive(BorshSerialize, BorshDeserialize, Serialize, Deserialize, Clone, PartialEq, Eq)]
#[serde(crate = "near_sdk::serde")]
pub enum SubscriptionStatus { Pending, Active, Expired, Suspended }

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct Subscription {
    pub account_id: AccountId,
    pub plan: Plan,
    pub status: SubscriptionStatus,
    pub started_at_ns: u64,
    pub expires_at_ns: u64,
    pub total_paid: Balance,
    pub payment_count: u64,
    pub last_payment_id: u64,
}

#[derive(BorshSerialize, BorshDeserialize, Clone)]
pub struct PendingPayment {
    pub account_id: AccountId,
    pub plan: Plan,
    pub amount: Balance,
    pub payment_id: u64,
    pub previous_expires_at_ns: u64,
}

#[derive(Serialize, Deserialize)]
#[serde(crate = "near_sdk::serde")]
pub struct PlanConfigView { pub monthly_price: U128, pub yearly_price: U128, pub paused: bool, pub treasury_id: AccountId }

#[derive(Serialize, Deserialize)]
#[serde(crate = "near_sdk::serde")]
pub struct SubscriptionView {
    pub account_id: AccountId,
    pub plan: Plan,
    pub status: SubscriptionStatus,
    pub started_at_ns: u64,
    pub expires_at_ns: u64,
    pub total_paid: U128,
    pub payment_count: u64,
    pub last_payment_id: u64,
}

#[derive(Serialize, Deserialize)]
#[serde(crate = "near_sdk::serde")]
pub struct PaymentStarted { pub payment_id: u64, pub plan: Plan, pub amount: U128, pub account_id: AccountId }

#[ext_contract(ext_self)]
#[allow(dead_code)]
trait ExtSelf { fn on_treasury_transfer(&mut self, payment_id: u64) -> bool; fn on_refund(&mut self, payment_id: u64) -> bool; }

#[near_bindgen]
#[derive(BorshSerialize, BorshDeserialize, PanicOnDefault)]
pub struct Contract {
    pub owner_id: AccountId,
    pub treasury_id: AccountId,
    pub monthly_price: Balance,
    pub yearly_price: Balance,
    pub paused: bool,
    pub next_payment_id: u64,
    pub subscriptions: LookupMap<AccountId, Subscription>,
    pub pending: UnorderedMap<u64, PendingPayment>,
}

#[near_bindgen]
impl Contract {
    #[init]
    pub fn new(owner_id: AccountId, treasury_id: AccountId, monthly_price: U128, yearly_price: U128) -> Self {
        assert!(!env::state_exists(), "Already initialized");
        assert!(monthly_price.0 > 0 && yearly_price.0 > 0, "Prices must be positive");
        Self { owner_id, treasury_id, monthly_price: monthly_price.0, yearly_price: yearly_price.0, paused: false,
            next_payment_id: 1, subscriptions: LookupMap::new(b"s".to_vec()), pending: UnorderedMap::new(b"p".to_vec()) }
    }

    #[payable]
    pub fn subscribe(&mut self, plan: Plan) -> PaymentStarted { self.start_payment(plan, false) }

    #[payable]
    pub fn renew(&mut self, plan: Plan) -> PaymentStarted { self.start_payment(plan, true) }

    pub fn get_plan_config(&self) -> PlanConfigView { PlanConfigView { monthly_price: U128(self.monthly_price), yearly_price: U128(self.yearly_price), paused: self.paused, treasury_id: self.treasury_id.clone() } }

    pub fn get_subscription(&self, account_id: AccountId) -> Option<SubscriptionView> {
        self.subscriptions.get(&account_id).map(|s| self.subscription_view(s))
    }

    pub fn is_active(&self, account_id: AccountId) -> bool {
        self.subscriptions.get(&account_id).map(|s| s.status == SubscriptionStatus::Active && s.expires_at_ns > env::block_timestamp()).unwrap_or(false)
    }

    pub fn set_price(&mut self, monthly_price: U128, yearly_price: U128) { self.assert_owner(); assert!(monthly_price.0 > 0 && yearly_price.0 > 0, "Prices must be positive"); self.monthly_price = monthly_price.0; self.yearly_price = yearly_price.0; }
    pub fn pause(&mut self) { self.assert_owner(); self.paused = true; }
    pub fn unpause(&mut self) { self.assert_owner(); self.paused = false; }
    pub fn withdraw(&mut self, amount: Option<U128>, receiver_id: Option<AccountId>) -> Promise { self.assert_owner(); let value = amount.map(|v| v.0).unwrap_or_else(|| env::account_balance().as_yoctonear().saturating_sub(env::storage_usage() as u128 * env::storage_byte_cost().as_yoctonear())); Promise::new(receiver_id.unwrap_or_else(|| self.treasury_id.clone())).transfer(NearToken::from_yoctonear(value)) }

    fn start_payment(&mut self, plan: Plan, renewal: bool) -> PaymentStarted {
        assert!(env::signer_account_id() == env::predecessor_account_id(), "Direct wallet call required");
        assert!(!self.paused, "Subscriptions are paused");
        let account_id = env::predecessor_account_id();
        assert!(self.pending.values().all(|p| p.account_id != account_id), "A payment is already pending");
        let amount = self.price(&plan);
        assert_eq!(env::attached_deposit().as_yoctonear(), amount, "Attach the exact plan price");
        let current = self.subscriptions.get(&account_id);
        if renewal { assert!(current.as_ref().map(|s| s.status == SubscriptionStatus::Active && s.expires_at_ns > env::block_timestamp()).unwrap_or(false), "No active subscription to renew"); }
        let previous_expires = current.as_ref().map(|s| s.expires_at_ns).unwrap_or(0);
        let payment_id = self.next_payment_id; self.next_payment_id += 1;
        self.pending.insert(&payment_id, &PendingPayment { account_id: account_id.clone(), plan: plan.clone(), amount, payment_id, previous_expires_at_ns: previous_expires });
        Promise::new(self.treasury_id.clone()).transfer(NearToken::from_yoctonear(amount))
            .then(ext_self::ext(env::current_account_id()).with_static_gas(CALLBACK_GAS).with_attached_deposit(NearToken::from_yoctonear(NO_DEPOSIT)).on_treasury_transfer(payment_id));
        PaymentStarted { payment_id, plan, amount: U128(amount), account_id }
    }

    #[private]
    pub fn on_treasury_transfer(&mut self, payment_id: u64) -> bool {
        assert_eq!(env::promise_results_count(), 1, "Expected one promise result");
        let Some(payment) = self.pending.remove(&payment_id) else { return false; };
        if matches!(env::promise_result(0), PromiseResult::Successful(_)) {
            let now = env::block_timestamp();
            let old = self.subscriptions.get(&payment.account_id);
            let active_start = old.as_ref().map(|s| s.expires_at_ns.max(now)).unwrap_or(now);
            let started_at = old.as_ref().map(|s| s.started_at_ns).unwrap_or(now);
            let sub = Subscription { account_id: payment.account_id.clone(), plan: payment.plan.clone(), status: SubscriptionStatus::Active, started_at_ns: started_at, expires_at_ns: active_start + payment.plan.duration_ns(), total_paid: old.as_ref().map(|s| s.total_paid).unwrap_or(0) + payment.amount, payment_count: old.as_ref().map(|s| s.payment_count).unwrap_or(0) + 1, last_payment_id: payment_id };
            self.subscriptions.insert(&payment.account_id, &sub);
            env::log_str(&format!(r#"{{"standard":"stock-screener","version":"1.0.0","event":"subscription_activated","payment_id":"{}","account_id":"{}","expires_at_ns":"{}"}}"#, payment_id, payment.account_id, sub.expires_at_ns));
            true
        } else {
            Promise::new(payment.account_id).transfer(NearToken::from_yoctonear(payment.amount)).then(ext_self::ext(env::current_account_id()).with_static_gas(REFUND_GAS).with_attached_deposit(NearToken::from_yoctonear(NO_DEPOSIT)).on_refund(payment_id));
            false
        }
    }

    #[private]
    pub fn on_refund(&mut self, _payment_id: u64) -> bool { matches!(env::promise_result(0), PromiseResult::Successful(_)) }

    fn price(&self, plan: &Plan) -> Balance { match plan { Plan::Monthly => self.monthly_price, Plan::Yearly => self.yearly_price } }
    fn subscription_view(&self, s: Subscription) -> SubscriptionView { let mut status = s.status; if status == SubscriptionStatus::Active && s.expires_at_ns <= env::block_timestamp() { status = SubscriptionStatus::Expired; } SubscriptionView { account_id: s.account_id, plan: s.plan, status, started_at_ns: s.started_at_ns, expires_at_ns: s.expires_at_ns, total_paid: U128(s.total_paid), payment_count: s.payment_count, last_payment_id: s.last_payment_id } }
    fn assert_owner(&self) { assert_eq!(env::predecessor_account_id(), self.owner_id, "Owner only"); }
}
