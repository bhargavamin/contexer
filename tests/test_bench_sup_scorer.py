from benchmarks.score import sup_current_score as s

def test_correct_current():            assert s("DynamoDB\nChosen for horizontal write scaling.") == "pass"
def test_correct_with_history_below(): assert s("DynamoDB\nWe migrated from Postgres for write scaling.") == "pass"
def test_wrong_superseded():           assert s("Postgres\nTransactional integrity under concurrent writes.") == "fail"
def test_hedged_both_on_first_line():  assert s("Postgres (moving to DynamoDB)\nMigration in progress.") == "review"
def test_unparseable():                assert s("It depends on the workload.") == "review"
def test_empty():                      assert s("") == "review"
def test_case_insensitive():           assert s("dynamodb\nscaling") == "pass"
