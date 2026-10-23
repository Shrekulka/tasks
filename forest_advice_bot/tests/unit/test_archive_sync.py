from services.archive_sync import plan_archive_sync


def test_new_file_is_added():
    plan = plan_archive_sync(disk={"manual.pdf": "sha-new"}, archive_known={})

    assert plan.add == {"sha-new": "manual.pdf"}
    assert plan.promote == {}
    assert plan.relink == {}
    assert plan.remove == frozenset()
    assert plan.duplicates == {}


def test_unchanged_file_is_ignored():
    plan = plan_archive_sync(disk={"a.pdf": "sha-1"}, archive_known={"sha-1": "a.pdf"})

    assert plan == plan_archive_sync(disk={}, archive_known={}) or (
        plan.add == {} and plan.relink == {} and plan.remove == frozenset()
    )


def test_renamed_file_is_relinked():
    plan = plan_archive_sync(disk={"guide.pdf": "sha-1"}, archive_known={"sha-1": "manual.pdf"})

    assert plan.add == {}
    assert plan.relink == {"sha-1": "guide.pdf"}
    assert plan.remove == frozenset()


def test_deleted_file_is_removed():
    plan = plan_archive_sync(disk={}, archive_known={"sha-1": "manual.pdf"})

    assert plan.remove == frozenset({"sha-1"})


def test_changed_content_adds_new_hash_and_removes_old():
    plan = plan_archive_sync(
        disk={"manual.pdf": "sha-new"}, archive_known={"sha-old": "manual.pdf"}
    )

    assert plan.add == {"sha-new": "manual.pdf"}
    assert plan.remove == frozenset({"sha-old"})


def test_duplicate_files_are_detected():
    plan = plan_archive_sync(
        disk={"manual.pdf": "sha-1", "copy.pdf": "sha-1"},
        archive_known={},
    )

    assert plan.add == {"sha-1": "copy.pdf"}
    assert plan.duplicates == {"sha-1": ["manual.pdf"]}


def test_missing_canonical_path_uses_existing_copy():
    plan = plan_archive_sync(disk={"copy.pdf": "sha-1"}, archive_known={"sha-1": "manual.pdf"})

    assert plan.relink == {"sha-1": "copy.pdf"}
    assert plan.remove == frozenset()


def test_user_doc_is_promoted_when_archive_gets_same_file():
    plan = plan_archive_sync(
        disk={"tips.txt": "sha-u"},
        archive_known={},
        user_known=frozenset({"sha-u"}),
    )

    assert plan.promote == {"sha-u": "tips.txt"}
    assert plan.add == {}


def test_user_docs_are_never_removed_by_sync():
    plan = plan_archive_sync(
        disk={},
        archive_known={"sha-a": "a.txt"},
        user_known=frozenset({"sha-u"}),
    )

    assert plan.remove == frozenset({"sha-a"})
    assert "sha-u" not in plan.remove
