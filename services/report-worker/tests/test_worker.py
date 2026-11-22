from src import worker


def test_queue_name():
    assert worker.QUEUE == "reports.requests"
