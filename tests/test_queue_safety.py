from __future__ import annotations

import asyncio

from app.jobs import CLAIM_JOB_LUA, set_job_running
from app.worker import acknowledge_and_delete


class FakeClaimRedis:
    def __init__(self, status: str = "queued"):
        self.status = status
        self.calls = 0

    async def eval(self, script, number_of_keys, job_key, now):
        assert script == CLAIM_JOB_LUA
        assert number_of_keys == 1
        assert job_key.endswith("00000000-0000-4000-8000-000000000001")
        self.calls += 1
        if self.status not in {"queued", "running"}:
            return [self.status, "", ""]
        self.status = "running"
        return ["running", '{"piston_language":"c++","version":"17.0.0"}', "learner-1"]


class FakePipeline:
    def __init__(self):
        self.operations = []
        self.transaction = False

    def xack(self, stream, group, message_id):
        self.operations.append(("xack", stream, group, message_id))
        return self

    def xdel(self, stream, message_id):
        self.operations.append(("xdel", stream, message_id))
        return self

    async def execute(self):
        return [1, 1]


class FakePipelineRedis:
    def __init__(self):
        self.pipe = FakePipeline()

    def pipeline(self, transaction=True):
        self.pipe.transaction = transaction
        return self.pipe


def test_job_claim_transitions_queued_to_running_atomically():
    redis = FakeClaimRedis("queued")
    job = asyncio.run(set_job_running(redis, "00000000-0000-4000-8000-000000000001"))
    assert redis.calls == 1
    assert redis.status == "running"
    assert job == {
        "status": "running",
        "payload": {"piston_language": "c++", "version": "17.0.0"},
        "owner_id": "learner-1",
    }


def test_cancelled_job_is_not_claimed_for_execution():
    redis = FakeClaimRedis("cancelled")
    job = asyncio.run(set_job_running(redis, "00000000-0000-4000-8000-000000000001"))
    assert job == {"status": "cancelled", "payload": None}
    assert redis.status == "cancelled"


def test_stream_acknowledgement_and_deletion_share_one_transaction():
    redis = FakePipelineRedis()
    asyncio.run(acknowledge_and_delete(redis, "1750000000000-0"))
    assert redis.pipe.transaction is True
    assert [operation[0] for operation in redis.pipe.operations] == ["xack", "xdel"]
