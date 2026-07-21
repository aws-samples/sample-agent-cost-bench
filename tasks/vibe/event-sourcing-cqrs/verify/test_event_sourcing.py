"""
Harness tests for event-sourcing-cqrs.

Tests the complete event-sourcing/CQRS system: event store, aggregates,
projections, command handlers, snapshots, and CLI.

WORKSPACE is set to the model's output directory by the harness.
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

WS = Path(os.environ.get("WORKSPACE", "."))


# ---------------------------------------------------------------------------
# Helpers — dynamic import from the workspace
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def setup_path():
    ws_str = str(WS)
    if ws_str not in sys.path:
        sys.path.insert(0, ws_str)
    yield
    if ws_str in sys.path:
        sys.path.remove(ws_str)


def _import(module_name: str):
    """Import a module from the workspace, raising skip if not found."""
    try:
        return importlib.import_module(module_name)
    except (ImportError, ModuleNotFoundError):
        pytest.skip(f"{module_name}.py not found")


# ---------------------------------------------------------------------------
# 1. File existence checks
# ---------------------------------------------------------------------------

def test_events_py_exists():
    assert (WS / "events.py").exists(), "events.py not produced"


def test_event_store_py_exists():
    assert (WS / "event_store.py").exists(), "event_store.py not produced"


def test_aggregates_py_exists():
    assert (WS / "aggregates.py").exists(), "aggregates.py not produced"


def test_projections_py_exists():
    assert (WS / "projections.py").exists(), "projections.py not produced"


def test_command_handler_py_exists():
    assert (WS / "command_handler.py").exists(), "command_handler.py not produced"


def test_snapshots_py_exists():
    assert (WS / "snapshots.py").exists(), "snapshots.py not produced"


def test_cli_py_exists():
    assert (WS / "cli.py").exists(), "cli.py not produced"


# ---------------------------------------------------------------------------
# 2. Event Store tests
# ---------------------------------------------------------------------------

def test_event_store_append_and_retrieve():
    """EventStore can append and retrieve events by aggregate_id."""
    events_mod = _import("events")
    store_mod = _import("event_store")

    store = store_mod.EventStore()
    agg_id = str(uuid.uuid4())

    # Create an event - try different patterns the model might use
    event = None
    if hasattr(events_mod, "Event"):
        event = events_mod.Event(
            event_id=str(uuid.uuid4()),
            aggregate_id=agg_id,
            event_type="AccountOpened",
            timestamp="2024-01-01T00:00:00",
            version=1,
            payload={"owner_name": "Alice", "initial_balance": 100.0},
        )
    else:
        pytest.fail("No Event class found in events.py")

    store.append(event)
    retrieved = store.get_events(agg_id)
    assert len(retrieved) == 1
    evt = retrieved[0]
    assert evt.aggregate_id == agg_id
    assert evt.event_type == "AccountOpened"


def test_event_store_concurrency_error():
    """EventStore raises ConcurrencyError on duplicate aggregate_id+version."""
    events_mod = _import("events")
    store_mod = _import("event_store")

    store = store_mod.EventStore()
    agg_id = str(uuid.uuid4())

    event1 = events_mod.Event(
        event_id=str(uuid.uuid4()),
        aggregate_id=agg_id,
        event_type="AccountOpened",
        timestamp="2024-01-01T00:00:00",
        version=1,
        payload={"owner_name": "Alice", "initial_balance": 100.0},
    )
    event2 = events_mod.Event(
        event_id=str(uuid.uuid4()),
        aggregate_id=agg_id,
        event_type="MoneyDeposited",
        timestamp="2024-01-01T00:01:00",
        version=1,  # duplicate version
        payload={"amount": 50.0, "description": "duplicate"},
    )
    store.append(event1)
    with pytest.raises(store_mod.ConcurrencyError):
        store.append(event2)


def test_event_store_after_version_filter():
    """get_events(after_version=N) returns only events with version > N."""
    events_mod = _import("events")
    store_mod = _import("event_store")

    store = store_mod.EventStore()
    agg_id = str(uuid.uuid4())

    for v in range(1, 4):
        e = events_mod.Event(
            event_id=str(uuid.uuid4()),
            aggregate_id=agg_id,
            event_type="MoneyDeposited",
            timestamp=f"2024-01-01T00:0{v}:00",
            version=v,
            payload={"amount": 10.0, "description": f"dep {v}"},
        )
        store.append(e)

    filtered = store.get_events(agg_id, after_version=1)
    assert len(filtered) == 2
    assert all(e.version > 1 for e in filtered)


def test_event_store_get_events_by_type():
    """get_events_by_type filters events across all aggregates."""
    events_mod = _import("events")
    store_mod = _import("event_store")

    store = store_mod.EventStore()

    for i in range(3):
        agg_id = str(uuid.uuid4())
        e = events_mod.Event(
            event_id=str(uuid.uuid4()),
            aggregate_id=agg_id,
            event_type="AccountOpened",
            timestamp=f"2024-01-01T00:0{i}:00",
            version=1,
            payload={"owner_name": f"User{i}", "initial_balance": 0.0},
        )
        store.append(e)

    opened_events = store.get_events_by_type("AccountOpened")
    assert len(opened_events) == 3


# ---------------------------------------------------------------------------
# 3. Aggregate tests
# ---------------------------------------------------------------------------

def _open_account(agg_mod, owner_name, initial_balance):
    """Helper to open an account handling multiple implementation patterns."""
    import inspect
    method = getattr(agg_mod.BankAccount, "open_account", None)
    if method is None:
        pytest.fail("BankAccount has no open_account method")

    # Pattern 1: classmethod/staticmethod with (owner_name, initial_balance)
    try:
        result = agg_mod.BankAccount.open_account(owner_name, initial_balance)
        if isinstance(result, agg_mod.BankAccount):
            return result
    except TypeError:
        pass

    # Pattern 2: classmethod with (account_id, owner_name, initial_balance)
    try:
        result = agg_mod.BankAccount.open_account(str(uuid.uuid4()), owner_name, initial_balance)
        if isinstance(result, agg_mod.BankAccount):
            return result
    except TypeError:
        pass

    # Pattern 3: instance method — create instance first, then call open_account
    try:
        instance = agg_mod.BankAccount()
        result = instance.open_account(owner_name, initial_balance)
        if isinstance(result, agg_mod.BankAccount):
            return result
        if hasattr(instance, 'owner_name') and instance.owner_name == owner_name:
            return instance
    except (TypeError, AttributeError):
        pass

    # Pattern 4: instance method with account_id
    try:
        instance = agg_mod.BankAccount()
        result = instance.open_account(str(uuid.uuid4()), owner_name, initial_balance)
        if isinstance(result, agg_mod.BankAccount):
            return result
        if hasattr(instance, 'owner_name') and instance.owner_name == owner_name:
            return instance
    except (TypeError, AttributeError):
        pass

    pytest.fail(f"Could not call BankAccount.open_account with owner={owner_name!r}, balance={initial_balance}")


def test_aggregate_open_account():
    """BankAccount aggregate can open an account and produce events."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Alice", 100.0)
    assert account is not None
    assert account.owner_name == "Alice"
    assert account.balance == 100.0
    assert account.is_open is True
    assert len(account.pending_events) >= 1


def test_aggregate_deposit():
    """BankAccount can deposit money."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Bob", 50.0)
    account.deposit(25.0, "Birthday gift")

    assert account.balance == 75.0
    # Should have at least 2 pending events (open + deposit)
    assert len(account.pending_events) >= 2


def test_aggregate_withdraw():
    """BankAccount can withdraw money when sufficient funds exist."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Charlie", 100.0)
    account.withdraw(30.0, "Groceries")

    assert account.balance == 70.0


def test_aggregate_withdraw_insufficient_funds():
    """BankAccount raises DomainError on overdraft."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Dave", 50.0)
    with pytest.raises(agg_mod.DomainError):
        account.withdraw(100.0, "Too much")


def test_aggregate_close_account():
    """BankAccount can be closed when balance is zero."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Eve", 0.0)
    account.close_account("No longer needed")

    assert account.is_open is False


def test_aggregate_close_nonzero_balance_fails():
    """BankAccount raises DomainError when closing with non-zero balance."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Frank", 100.0)
    with pytest.raises(agg_mod.DomainError):
        account.close_account("Want to close")


def test_aggregate_save_persists_events():
    """aggregate.save() persists pending events to the store."""
    agg_mod = _import("aggregates")
    store_mod = _import("event_store")

    store = store_mod.EventStore()
    account = _open_account(agg_mod, "Grace", 200.0)
    account.deposit(50.0, "Bonus")
    account.save(store)

    assert len(account.pending_events) == 0
    events = store.get_events(account.account_id)
    assert len(events) >= 2


def test_aggregate_load_from_store():
    """BankAccount.load() reconstitutes aggregate from event store."""
    agg_mod = _import("aggregates")
    store_mod = _import("event_store")

    store = store_mod.EventStore()
    account = _open_account(agg_mod, "Heidi", 300.0)
    account.deposit(100.0, "Salary")
    account.withdraw(50.0, "Rent")
    account.save(store)

    # Reload from store
    loaded = agg_mod.BankAccount.load(store, account.account_id)
    assert loaded.balance == 350.0
    assert loaded.owner_name == "Heidi"
    assert loaded.is_open is True


# ---------------------------------------------------------------------------
# 4. Command Handler tests
# ---------------------------------------------------------------------------

def test_command_handler_open_account():
    """CommandHandler.handle_open_account creates an account."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    summary_proj = proj_mod.AccountSummaryProjection()
    tx_proj = proj_mod.TransactionLogProjection()
    handler = handler_mod.CommandHandler(store, [summary_proj, tx_proj])

    account_id = handler.handle_open_account("Ivan", 500.0)
    assert account_id is not None
    assert isinstance(account_id, str)
    assert len(account_id) > 0


def test_command_handler_deposit_returns_balance():
    """CommandHandler.handle_deposit returns new balance."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    handler = handler_mod.CommandHandler(store, [proj_mod.AccountSummaryProjection()])

    account_id = handler.handle_open_account("Judy", 100.0)
    new_balance = handler.handle_deposit(account_id, 50.0, "Tip")
    assert new_balance == 150.0


def test_command_handler_withdraw_returns_balance():
    """CommandHandler.handle_withdraw returns new balance."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    handler = handler_mod.CommandHandler(store, [proj_mod.AccountSummaryProjection()])

    account_id = handler.handle_open_account("Karl", 200.0)
    new_balance = handler.handle_withdraw(account_id, 75.0, "Shopping")
    assert new_balance == 125.0


def test_command_handler_transfer():
    """CommandHandler.handle_transfer moves money between accounts."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")
    agg_mod = _import("aggregates")

    store = store_mod.EventStore()
    summary_proj = proj_mod.AccountSummaryProjection()
    handler = handler_mod.CommandHandler(store, [summary_proj])

    source_id = handler.handle_open_account("Liam", 500.0)
    target_id = handler.handle_open_account("Mia", 100.0)

    transfer_id = handler.handle_transfer(source_id, target_id, 200.0, "Repayment")
    assert transfer_id is not None

    # Verify balances via loading aggregates
    source = agg_mod.BankAccount.load(store, source_id)
    target = agg_mod.BankAccount.load(store, target_id)
    assert source.balance == 300.0
    assert target.balance == 300.0


# ---------------------------------------------------------------------------
# 5. Projection tests
# ---------------------------------------------------------------------------

def test_account_summary_projection():
    """AccountSummaryProjection tracks account state from events."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    summary_proj = proj_mod.AccountSummaryProjection()
    handler = handler_mod.CommandHandler(store, [summary_proj])

    account_id = handler.handle_open_account("Nina", 100.0)
    handler.handle_deposit(account_id, 50.0, "Gift")

    summary = summary_proj.get_summary(account_id)
    assert summary is not None
    assert summary["owner_name"] == "Nina"
    assert summary["balance"] == 150.0
    assert summary["is_open"] is True


def test_account_summary_total_balance():
    """AccountSummaryProjection.get_total_balance sums open accounts."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    summary_proj = proj_mod.AccountSummaryProjection()
    handler = handler_mod.CommandHandler(store, [summary_proj])

    handler.handle_open_account("Oscar", 100.0)
    handler.handle_open_account("Pam", 200.0)

    total = summary_proj.get_total_balance()
    assert total == 300.0


def test_transaction_log_projection():
    """TransactionLogProjection records transaction history."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    tx_proj = proj_mod.TransactionLogProjection()
    handler = handler_mod.CommandHandler(store, [tx_proj])

    account_id = handler.handle_open_account("Quinn", 100.0)
    handler.handle_deposit(account_id, 50.0, "Allowance")
    handler.handle_withdraw(account_id, 30.0, "Coffee")

    transactions = tx_proj.get_transactions(account_id)
    assert len(transactions) >= 2  # at least deposit + withdrawal

    # Most recent first
    types = [t["type"] for t in transactions]
    assert "withdrawal" in types
    assert "deposit" in types


def test_transaction_log_limit():
    """TransactionLogProjection.get_transactions respects limit param."""
    handler_mod = _import("command_handler")
    store_mod = _import("event_store")
    proj_mod = _import("projections")

    store = store_mod.EventStore()
    tx_proj = proj_mod.TransactionLogProjection()
    handler = handler_mod.CommandHandler(store, [tx_proj])

    account_id = handler.handle_open_account("Rachel", 100.0)
    for i in range(5):
        handler.handle_deposit(account_id, 10.0, f"Deposit {i}")

    transactions = tx_proj.get_transactions(account_id, limit=3)
    assert len(transactions) == 3


# ---------------------------------------------------------------------------
# 6. Snapshot tests
# ---------------------------------------------------------------------------

def test_snapshot_save_and_restore():
    """SnapshotStore can save and retrieve snapshots."""
    snap_mod = _import("snapshots")

    snap_store = snap_mod.SnapshotStore()
    agg_id = str(uuid.uuid4())
    state = {"owner_name": "Sam", "balance": 500.0, "is_open": True}

    snap_store.save_snapshot(agg_id, 5, state)
    result = snap_store.get_snapshot(agg_id)
    assert result is not None
    version, restored_state = result
    assert version == 5
    assert restored_state["balance"] == 500.0


def test_aggregate_to_snapshot():
    """BankAccount.to_snapshot() produces a serializable dict."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Tina", 250.0)
    account.deposit(50.0, "Bonus")

    snapshot = account.to_snapshot()
    assert isinstance(snapshot, dict)
    assert snapshot.get("balance") == 300.0 or snapshot.get("_balance") == 300.0


def test_aggregate_from_snapshot():
    """BankAccount.from_snapshot() restores state from a snapshot dict."""
    agg_mod = _import("aggregates")

    account = _open_account(agg_mod, "Uma", 400.0)
    account.deposit(100.0, "Interest")

    snapshot = account.to_snapshot()
    restored = agg_mod.BankAccount.from_snapshot(snapshot)

    assert restored.balance == 500.0
    assert restored.owner_name == "Uma"


# ---------------------------------------------------------------------------
# 7. CLI tests
# ---------------------------------------------------------------------------

def test_cli_open_command():
    """CLI 'open' subcommand produces JSON output with account_id."""
    result = subprocess.run(
        [sys.executable, str(WS / "cli.py"), "open", "--owner", "Victor", "--balance", "100"],
        capture_output=True, text=True, timeout=15, cwd=WS,
    )
    assert result.returncode == 0, f"CLI failed: {result.stderr}"
    output = json.loads(result.stdout.strip())
    assert "account_id" in output or "id" in output


def test_cli_summary_command():
    """CLI 'summary' subcommand produces JSON output."""
    # First open an account via CLI
    open_result = subprocess.run(
        [sys.executable, str(WS / "cli.py"), "open", "--owner", "Wendy", "--balance", "200"],
        capture_output=True, text=True, timeout=15, cwd=WS,
    )
    assert open_result.returncode == 0, f"open failed: {open_result.stderr}"

    # Then get summary (no --account means all accounts for this in-memory session)
    # Since each CLI invocation is independent with in-memory store, just test it runs
    summary_result = subprocess.run(
        [sys.executable, str(WS / "cli.py"), "summary"],
        capture_output=True, text=True, timeout=15, cwd=WS,
    )
    assert summary_result.returncode == 0, f"summary failed: {summary_result.stderr}"
    output = json.loads(summary_result.stdout.strip())
    assert isinstance(output, (dict, list))
