Implementation priorities:

P0 — Preserve current functionality
1. Refactor the existing bitable_trigger_worker.py into the proposed structure.
2. Do not change the current Bitable trigger mechanism:
       Button
         → Automation
         → 「已请求」 = true
         → worker polling
3. Keep the existing three material fields working:
       品名
       喷嘴温度
       最大体积流速
4. Keep successful processing updating:
       状态 = 审核中
5. Keep incomplete input from being submitted.

P1 — Domain model
6. Implement MaterialProfile as the canonical domain model.
7. MaterialProfile must not depend on Bitable SDK or Git SDK.
8. Implement validation and JSON serialization.
9. Separate MaterialProfile from MaterialSubmission.

P2 — Bitable adapter
10. Move all Lark/Bitable SDK code into BitableClient.
11. Business logic must not directly call Lark SDK.
12. Implement list_pending_records() using Bitable-side filtering
    instead of fetching the whole table.
13. Centralize Bitable field-name mapping.

P3 — Submission state
14. Implement SubmissionStatus.
15. Add Bitable fields:
       提交 ID
       PR URL
       错误信息
       状态
16. Design explicit state transitions.
17. Do not allow accidental duplicate processing.

P4 — Idempotency
18. Every submission must have a stable submission_id.
19. Retrying the same submission must NOT create another PR.
20. Git branch name must be deterministic from submission_id.
21. Before creating a branch/PR, check whether the submission
    has already been submitted.
22. Handle the case:
       Git succeeded
       Bitable update failed
    without creating a duplicate PR on retry.

P5 — Git adapter
23. Git provider implementation must be isolated in GitRepository.
24. SubmissionService must not contain GitHub/Gitea HTTP/API details.
25. Implement:
       create_branch()
       write_profile()
       commit()
       push()
       create_pull_request()
       find_submission()
26. submit_profile() should be idempotent.

P6 — Error handling
27. Distinguish:
       validation/permanent errors
       transient/retryable errors
28. Do not permanently mark a submission FAILED for a temporary
    Git/network failure.
29. Add retry_count / last_error / last_processed_at if needed.
30. A worker-level exception must not terminate the daemon.

P7 — Tests
31. Add unit tests for MaterialProfile.
32. Add tests for state transitions.
33. Add tests proving duplicate submission does not create
    duplicate PRs.
34. Add tests for:
       validation failure
       Git failure
       Bitable update failure
       retry after partial success.
35. Mock BitableClient and GitRepository in service tests.

Do not prematurely add a database.
Git repository remains the source of truth for material profiles.