# Changelog

## [0.44.0](https://github.com/bhargavamin/contexer/compare/v0.43.0...v0.44.0) (2026-08-30)


### Features

* **cli:** add `contexer upgrade` and the terminal update backstop ([2808c4f](https://github.com/bhargavamin/contexer/commit/2808c4f4abe4fc1ae97f2b1f4fe15a7d4bb0d339))
* tell developers when a new Contexer release exists ([613aa3a](https://github.com/bhargavamin/contexer/commit/613aa3a8a4cb46e64778ef8976c374538220b9c6))
* **updates:** deliver new-release notices through a per-adapter seam ([7fb8e62](https://github.com/bhargavamin/contexer/commit/7fb8e627f0cd19f5e25107ee0c546058726a1bf5))


### Bug Fixes

* **cli:** keep share help read-only ([d03a173](https://github.com/bhargavamin/contexer/commit/d03a173b540317bf11b7424bccd2d5f3909c6f4e))
* **cli:** keep share help read-only ([4b882a1](https://github.com/bhargavamin/contexer/commit/4b882a1882198207e7b3af9bb575a207876ee6d5))


### Documentation

* align Phase 0 gate status ([261cd2e](https://github.com/bhargavamin/contexer/commit/261cd2e383db44ac77af7530f382baee4c2a2319))
* complete decision-sharing Phase 0 ([92e7c12](https://github.com/bhargavamin/contexer/commit/92e7c12bd35290df634c8293e02ad6c994a2e362))
* document update delivery and correct two stale claims ([c17c246](https://github.com/bhargavamin/contexer/commit/c17c2466a36ce03031195de659b6b3bb477ac127))
* **evidence:** complete readiness closeout ([71c0f0c](https://github.com/bhargavamin/contexer/commit/71c0f0cea227a726f03156a47b45acfdc4e8316f))
* **evidence:** complete readiness closeout ([fe5e872](https://github.com/bhargavamin/contexer/commit/fe5e872d49886f5092cd9b3ee0936da6e3ae6b12))
* **evidence:** publish readiness review closeout ([d278102](https://github.com/bhargavamin/contexer/commit/d278102d2e2beaa1d01560b134408dca824f4661))
* **evidence:** publish readiness review closeout ([bb6ad9b](https://github.com/bhargavamin/contexer/commit/bb6ad9b9d8bdf6ffedd1e3e284d98b089f9d1a66))
* **evidence:** record closeout follow-up ([61ed51a](https://github.com/bhargavamin/contexer/commit/61ed51ae15d18789bda55af5a39a5a4fb5a47470))
* **evidence:** record final pointer correction ([18f2c7b](https://github.com/bhargavamin/contexer/commit/18f2c7b01706bc7c2c7fb8a2e1dd3c2391c95bf8))
* **evidence:** record final pointer correction ([3efcb09](https://github.com/bhargavamin/contexer/commit/3efcb090fde235bbf0be9811520f84384ff484f3))
* reconcile decision-sharing Phase 0 ([70f6f44](https://github.com/bhargavamin/contexer/commit/70f6f44c5ad8adbcea902371e0d053153cc177bb))
* reconcile decision-sharing Phase 0 ([c620d1e](https://github.com/bhargavamin/contexer/commit/c620d1e1437d9f777708447beb56e30937101390))

## [0.43.0](https://github.com/bhargavamin/contexer/compare/v0.42.0...v0.43.0) (2026-08-30)


### Features

* **evidence:** add event schema and pure validation for the evidence ledger ([66148d0](https://github.com/bhargavamin/contexer/commit/66148d0ae9f36bb3b3d0f44f3e459b2baaf9632b))
* **evidence:** add the per-event JSON spool storage engine ([d8f7554](https://github.com/bhargavamin/contexer/commit/d8f7554b6ec8c399dd6d465c6dc1a42e016ac976))
* **evidence:** bound deferred review attention ([e0ab4cf](https://github.com/bhargavamin/contexer/commit/e0ab4cf925e741710fd2fecc576fb99fd5814320))
* **evidence:** contain directive and anchor scope ([d7b4d90](https://github.com/bhargavamin/contexer/commit/d7b4d90394bfb4ce6986225a4531a1ee9961ed87))
* **evidence:** emit host-adapter events in shadow mode ([61e3975](https://github.com/bhargavamin/contexer/commit/61e397579e283e28862fad9eb8d7dd7ef731b547))
* **evidence:** group and score evidence into decision candidates ([ad3f00a](https://github.com/bhargavamin/contexer/commit/ad3f00aed7e8c239f91aec0fc43d7af1ea373714))
* **evidence:** harden decision-evidence readiness ([97c479c](https://github.com/bhargavamin/contexer/commit/97c479c86aeaf36ab1311298fd4837b8f37bc18e))
* **evidence:** preserve typed links and recurrences ([1c541dd](https://github.com/bhargavamin/contexer/commit/1c541dd6e38bc6d33cf7aa084e6745eda0021b70))
* **evidence:** quarantine foreign repository events ([3cbd3df](https://github.com/bhargavamin/contexer/commit/3cbd3df270c3165a6b85289fad1e0d601c066c65))
* **evidence:** read and record candidate checkpoints ([20893e8](https://github.com/bhargavamin/contexer/commit/20893e862a0d5ec198b8ec6f614de33a787f07aa))
* **evidence:** reconcile recorded evidence into decisions pending review ([fa72ccb](https://github.com/bhargavamin/contexer/commit/fa72ccb7a6a5d2551e09bc849f05aab0ac6ab277))
* **evidence:** report host capture coverage ([c663af3](https://github.com/bhargavamin/contexer/commit/c663af3a319b11b2adfbd344cb3b2dc7750e2da5))
* **evidence:** store events in an atomic per-repo sidecar ([58e8656](https://github.com/bhargavamin/contexer/commit/58e865610c8493361cf68a7fa3bc237c2d93b67b))
* **lifecycle:** add the proposed_lifecycle lane, retire and restore ([8d873bc](https://github.com/bhargavamin/contexer/commit/8d873bc8723df0ea08dfaf900493a8e8841c3b6c))
* **lifecycle:** expose retire, restore and dismiss on the CLI and MCP ([1d31263](https://github.com/bhargavamin/contexer/commit/1d3126319483978f5b6a6de48cef857fd00d1259))
* **policy:** add the evaluate_policy tool and the policy evaluate command ([c009c33](https://github.com/bhargavamin/contexer/commit/c009c33b24ef36084780dafb2b71ac7673ef377a))
* **policy:** add the policy request/result types and pure selection ([0dc5917](https://github.com/bhargavamin/contexer/commit/0dc59179410652987facb9f9adfe571d6d9dabae))
* **remote:** open the lifecycle wire gate against a validated Teams contract ([953d58b](https://github.com/bhargavamin/contexer/commit/953d58bcd951d0ef761b01f94f38245fb3ff5d49))
* **review:** preview evidence and policy impact ([42ae4fc](https://github.com/bhargavamin/contexer/commit/42ae4fca9d78c968cae85c95017e2cb2eebd6a80))
* **teams:** negotiate revision and lifecycle sync, gate closed ([8a0a7ec](https://github.com/bhargavamin/contexer/commit/8a0a7ec4bd89b9a1ef18f20265eca45c2df7a989))
* **teams:** render lifecycle divergence ([aac871a](https://github.com/bhargavamin/contexer/commit/aac871aadac63e0c3dc8f3e621faa2b7ab1b6f0d))


### Bug Fixes

* **adapters:** isolate hook python from cwd and converge stale hook commands ([8527bad](https://github.com/bhargavamin/contexer/commit/8527bad1c3b3e3c164b73b0ba0731a611d79530f))
* **adapters:** strip only contexer-owned hooks and tolerate null commands ([aad40ca](https://github.com/bhargavamin/contexer/commit/aad40cadb08c69c611912d6487cfd4b04dcb7ff6))
* **candidates:** attach a file change to a fileless directive seed ([65ae14c](https://github.com/bhargavamin/contexer/commit/65ae14c9700da7e78b96c9881157b45da14a1e87))
* **evidence:** close the spool, store and CLI residuals ([dc56ef8](https://github.com/bhargavamin/contexer/commit/dc56ef8e42e119cd36cd65073f85702112882c54))
* **evidence:** four review fixes to the spool engine ([d84c2c3](https://github.com/bhargavamin/contexer/commit/d84c2c3f96fe01501213613926845f78e9f9cab2))
* **evidence:** harden integrated readiness gates ([4cb5f44](https://github.com/bhargavamin/contexer/commit/4cb5f4447954c8610371acda122579c3ec12dff9))
* **evidence:** keep hold/finalize soft on every I/O failure ([a3d19ee](https://github.com/bhargavamin/contexer/commit/a3d19ee8d5f23f9ec0acd2d5e73513bf18f34976))
* **evidence:** keep inferred decisions reviewable end to end ([684c080](https://github.com/bhargavamin/contexer/commit/684c0807d5a394b0254af2923464b2ac9b0f4ccc))
* **evidence:** keep uncertain paths out of anchor candidates ([78e05aa](https://github.com/bhargavamin/contexer/commit/78e05aa12c2f50345d0d6e54e04c47df16e1f6d2))
* **evidence:** key every event by the repo path ([818d9f1](https://github.com/bhargavamin/contexer/commit/818d9f1c624e7197e7abab9d1075fcdf717edb3d))
* **evidence:** refuse to compact when the evidence lock is not held ([9b4c78d](https://github.com/bhargavamin/contexer/commit/9b4c78dd7b85bca20ff74e6a4a79979c519bc13d))
* **evidence:** return an error for an unhashable `kind` instead of raising ([e253c17](https://github.com/bhargavamin/contexer/commit/e253c1728e9f41f139f6be0aa194f62673dc9380))
* **evidence:** stop status reporting a reconciliation it never ran ([025ca08](https://github.com/bhargavamin/contexer/commit/025ca085a2c4088ce6f6721f1640f7f4e634d2c9))
* **evidence:** tell an absent spool from an unreadable one ([d61a8c8](https://github.com/bhargavamin/contexer/commit/d61a8c86d61195d94730a58f57e0d063ea5b7328))
* **evidence:** test a held candidate's recorded status against the spool's vocabulary ([5a2fe46](https://github.com/bhargavamin/contexer/commit/5a2fe463bacd097d8511482f4530023435c367e5))
* **evidence:** three cross-module defects and the merge-triage cleanup ([f5e22d5](https://github.com/bhargavamin/contexer/commit/f5e22d5ad0ee85e2208dbd463f53297f821ea7a7))
* **evidence:** three review fixes to the spool rewiring ([9542b5a](https://github.com/bhargavamin/contexer/commit/9542b5a8f9511d07f45e97a7015e12f952446a20))
* **guard:** state the fail-direction inversion where it will be read ([58bb423](https://github.com/bhargavamin/contexer/commit/58bb423b4ab0b8e8508dd0c07bd324dd65f7a568))
* **install:** converge legacy assistant wiring ([071204c](https://github.com/bhargavamin/contexer/commit/071204cda8825e697b6bd8a3d53f07d411b37527))
* **integration:** preserve adapter and lifecycle retry invariants ([3af0f2e](https://github.com/bhargavamin/contexer/commit/3af0f2efb6d6164119e1148239d9332c764e5cae))
* **integration:** verify evidence port against current baselines ([badb74a](https://github.com/bhargavamin/contexer/commit/badb74a1a302429f41fe34ea9f4318f5dd7785c5))
* **lifecycle:** bind reconsideration to the basis the evidence was classified at ([8def30e](https://github.com/bhargavamin/contexer/commit/8def30eec7c1110a5a00f2752ab85a39e88d7880))
* **lifecycle:** review inactive decision reconsideration ([83adc09](https://github.com/bhargavamin/contexer/commit/83adc09b1fe844bc86efe81f7d6ad05ea616690b))
* **lifecycle:** settle reconsiderations on their receipt, not on a shared record ([e09db8e](https://github.com/bhargavamin/contexer/commit/e09db8efa935e6ab8add89a375833e14bc108b7a))
* **policy:** report a non-string armed pattern instead of escaping ([daadd1c](https://github.com/bhargavamin/contexer/commit/daadd1c77be5b974e1a1c8ebe6a75276aecf6a6d))
* **policy:** scope rule judging to the files it selects, not the whole artifact ([bda8799](https://github.com/bhargavamin/contexer/commit/bda8799f5b470b48fee71c65c494fa6f935c27d4))
* **policy:** scrub the JSON result before encoding, not after ([4fce6da](https://github.com/bhargavamin/contexer/commit/4fce6da3c3a83a39534f7ecf810febf02d1707a4))
* **policy:** stop two gaps in the evaluator from reading as clean ([abe82b1](https://github.com/bhargavamin/contexer/commit/abe82b18d52dffa9d903d7ebabdef9f73dcbec7b))
* **policy:** three self-review fixes to the evaluate surfaces ([59350dd](https://github.com/bhargavamin/contexer/commit/59350dd504b11be248ad20f5ac89c4ef50e1a8e2))
* **reconcile:** derive resume dispositions from observed review state ([5f07135](https://github.com/bhargavamin/contexer/commit/5f07135e1160fc9084a4f93518884779e4fe02d7))
* **reconcile:** hold evidence across the materialize-before-hold crash window ([11a361a](https://github.com/bhargavamin/contexer/commit/11a361a9061ee46684cb72ffb4ee58c725a051c4))
* **reconcile:** settle lifecycle checkpoints on retirement, not on a revision ([8cf789b](https://github.com/bhargavamin/contexer/commit/8cf789b7f401f2da9f9f868cdaa7a410ed5ef81b))
* **reconcile:** take the reconcile lock only once there is work ([77a0450](https://github.com/bhargavamin/contexer/commit/77a0450c385d7ddbd3a994f059b26ffb7558bfa2))
* **reconcile:** time-scope shared dismissal receipts ([3cff720](https://github.com/bhargavamin/contexer/commit/3cff720a6f121253108860e53d2463bd5c29b910))
* **review:** say that approving a dormant armed rule re-activates it ([ba77e86](https://github.com/bhargavamin/contexer/commit/ba77e86bb456d5455174dd11b6782d0e4db714fd))
* **review:** stop the impact block promising anchors it never writes ([743cfc3](https://github.com/bhargavamin/contexer/commit/743cfc32d732aba19226e3c0cb17606161bdd44d))
* **share:** keep a lifecycle delta refused during a drain ([576e0a5](https://github.com/bhargavamin/contexer/commit/576e0a583b8d3a4dbcc749dbe235d4a77168abb0))
* **share:** record a blocked lifecycle delta only for a confirmed saved base ([e879109](https://github.com/bhargavamin/contexer/commit/e8791093e06e414f1ed0bc910e86094ed3f7a4f7))
* **spool:** file a durable receipt before orphan evidence is removed ([c191f63](https://github.com/bhargavamin/contexer/commit/c191f63aabc7224c3c05fd3a35ea38db2586a030))
* **store:** reject the harness usage-limit-reset notice as a constraint ([d4df4e9](https://github.com/bhargavamin/contexer/commit/d4df4e9bd91f46c78e7f5661a9e1a45e8083a932))
* **teams:** four self-review fixes to the lifecycle wire projection ([a9286e9](https://github.com/bhargavamin/contexer/commit/a9286e9b8174deb3e36e777e6440e86eeb9339d4))


### Performance Improvements

* **evidence:** index aggregation and reconcile at session start ([79dbe6f](https://github.com/bhargavamin/contexer/commit/79dbe6feb9d0b7d4f693b41925db7ee776df84ee))


### Documentation

* add evidence-capture and policy-evaluation implementation plan ([773b662](https://github.com/bhargavamin/contexer/commit/773b662200be358cbaf3b26724eefefe9689c9cd))
* **evidence:** correct the spool docs that still say hosts never reconcile ([cf9a43b](https://github.com/bhargavamin/contexer/commit/cf9a43b1c7d92e2ccc6c3483b5b25af3a34ccdb5))
* **evidence:** correct two spool docstrings the fixes made wrong ([b526669](https://github.com/bhargavamin/contexer/commit/b52666907ef34f0f19531a47e8232ef92fba5fce))
* **evidence:** finish the sweep of retired never-reconciles claims ([d42c6aa](https://github.com/bhargavamin/contexer/commit/d42c6aa52e5d31ec03a44b8c426f391ed583833b))
* **evidence:** record the hardening outcome across the architecture docs ([d425ac5](https://github.com/bhargavamin/contexer/commit/d425ac58a48697ace32d85d2d4ccea2238aba3e8))
* **lifecycle:** record the Issue 3 fallback partition in CLAUDE.md ([09c5e65](https://github.com/bhargavamin/contexer/commit/09c5e65d0576662d82d2486a07c43e9921df4bc0))
* **lifecycle:** record the Task 08 contract, evidence and open concerns ([b294aa3](https://github.com/bhargavamin/contexer/commit/b294aa31510561a9a07eed4e33284cda59e17f08))
* put the aggregation ceiling where it will be read, and fix three claims ([ce1da3b](https://github.com/bhargavamin/contexer/commit/ce1da3bc90f81ecaf0c96b2e016ce36219b8d9ea))
* record the evidence and policy modules in the architecture record ([2a8c834](https://github.com/bhargavamin/contexer/commit/2a8c834bd7021b54734fbe18d3593b205944e5b2))
* record the three fixes and the vocabulary owners in the architecture record ([e77bbc9](https://github.com/bhargavamin/contexer/commit/e77bbc9ca2979cd43dbfb64a9589999ee4279718))
* revise plan to per-event JSON evidence spool ([64ef8c4](https://github.com/bhargavamin/contexer/commit/64ef8c4ce153bfee5af22d430d146df36350127a))
* **server:** state reconcile_session's real per-host automatic checkpoints ([ff88681](https://github.com/bhargavamin/contexer/commit/ff88681fc33cc0482d5697888a0eeab71f1fcb77))
* **teams:** put the pre-flip checklist inline, with its counter-evidence ([2988707](https://github.com/bhargavamin/contexer/commit/298870714a1bff664cd7947b282bb293dd80fe7e))

## [0.42.0](https://github.com/bhargavamin/contexer/compare/v0.41.0...v0.42.0) (2026-08-29)


### Features

* console Sessions view - per-session decision transcript ([#256](https://github.com/bhargavamin/contexer/issues/256)) ([a66558e](https://github.com/bhargavamin/contexer/commit/a66558ef3fc82453e22a70b866a0deee77317fb9))
* console Sessions view rendering per-session decision transcripts ([1ec5002](https://github.com/bhargavamin/contexer/commit/1ec5002116c3dc8b8e60f17db6aeb05eecc3b4e3))
* link a session's transcript to its real Claude Code conversation when available ([df11bdf](https://github.com/bhargavamin/contexer/commit/df11bdf7aeb629c828eb149532f599f4e5ffe09b))
* link a session's transcript to its real Claude Code conversation when available ([d5a044d](https://github.com/bhargavamin/contexer/commit/d5a044d47a7ccb76ddaf76c09d42ec9a0047e6bf))
* per-session transcript reads in console_api (list_sessions, session_transcript) ([68164ae](https://github.com/bhargavamin/contexer/commit/68164ae08db8f150006c325dc3ab87b75543fc9a))


### Bug Fixes

* reject a session_id containing a path separator or '..' in the transcript route ([abbe78a](https://github.com/bhargavamin/contexer/commit/abbe78a9fdab2bef3a13c0bb168a1a5c57a6d251))
* session_transcript rejects an empty session id instead of matching arbitrarily ([db20161](https://github.com/bhargavamin/contexer/commit/db2016137a36067d287fcf643ef069889e60b465))
* stamp the host's real session id from CLAUDE_CODE_SESSION_ID when present ([6dac3ba](https://github.com/bhargavamin/contexer/commit/6dac3baaafd57acf651f8111148c83cfa9f324ec))
* stamp the host's real session id from CLAUDE_CODE_SESSION_ID when present ([5e96dbc](https://github.com/bhargavamin/contexer/commit/5e96dbce163f38dee8ddb6f83ca2ec8d97b39f07))


### Documentation

* add thin-MCP-tools design constraint ([#253](https://github.com/bhargavamin/contexer/issues/253)) ([70ca300](https://github.com/bhargavamin/contexer/commit/70ca300c4733eaacd13971546b42f9afadb7def8))
* add thin-MCP-tools design constraint ([#253](https://github.com/bhargavamin/contexer/issues/253)) ([d8092fd](https://github.com/bhargavamin/contexer/commit/d8092fdb8e2e90bc8b9561e6805b60a52646e599))

## [0.41.0](https://github.com/bhargavamin/contexer/compare/v0.40.0...v0.41.0) (2026-08-28)


### Features

* add ranked applicability retrieval (rank_applicable) ([45e708b](https://github.com/bhargavamin/contexer/commit/45e708b68474f262d565455e3fd23fea10f2927a))


### Bug Fixes

* **remote:** classify a rate limit as its own error type ([99d9fad](https://github.com/bhargavamin/contexer/commit/99d9fad46d0709753fd69d3488607654eb56d9d5))
* replace marker-presence hook migration with exact-command currency ([e7f33a6](https://github.com/bhargavamin/contexer/commit/e7f33a64a361d7164259acb9e266e1d61750af27))


### Documentation

* make CLAUDE.md the public contract ([6434fde](https://github.com/bhargavamin/contexer/commit/6434fdee8feb840066f84169e09a089745b78763))
* record the share seam, the rate-limit type and the outbox lock ([3c0724c](https://github.com/bhargavamin/contexer/commit/3c0724cbf6060b53298d458fbe543dc22fce9c9b))

## [0.40.0](https://github.com/bhargavamin/contexer/compare/v0.39.0...v0.40.0) (2026-08-24)


### Features

* **sidecars:** declare every sidecar name and lifetime in one module ([0e2a37d](https://github.com/bhargavamin/contexer/commit/0e2a37dfe562a6caba0842d5b194dc5ee565a832))


### Bug Fixes

* give the anchor field pair a single writer ([79ce9c0](https://github.com/bhargavamin/contexer/commit/79ce9c0e278418b94e287cdd6ad45f52a74bf7da))
* **server:** do not promise an outbox retry that was never recorded ([8bd867c](https://github.com/bhargavamin/contexer/commit/8bd867c5e4c94493fce942b506e8cdb3b4a0c8f1))
* **share:** refuse to overwrite an unreadable share retry queue ([93e7cc1](https://github.com/bhargavamin/contexer/commit/93e7cc1ac8179a0895f975587939cc8e1f0d0ea3))
* **share:** refuse to overwrite an unreadable share retry queue ([7c66b52](https://github.com/bhargavamin/contexer/commit/7c66b52a17a63ce36c96cdb27e173ec61c4cebe7))


### Documentation

* record the anchor field pair's single-writer invariant ([c469d43](https://github.com/bhargavamin/contexer/commit/c469d432ad3f37b2af68cc20757789c7d0567358))
* record the module boundary rules and refresh stale helper names ([911772d](https://github.com/bhargavamin/contexer/commit/911772db04c22ce8b8063c7542800038f25bb78c))
* record the outbox write rule and drop a duplicated clause list ([5b9b1f4](https://github.com/bhargavamin/contexer/commit/5b9b1f43d039170a4f153934eac57f76dfe04b67))
* record the sidecar declaration and its three-direction test ([7dd4905](https://github.com/bhargavamin/contexer/commit/7dd490597c8940896e6226eef38ac3bd6d462473))
* **redact:** state that redaction is egress-only, not at capture ([9ec9377](https://github.com/bhargavamin/contexer/commit/9ec937700e1d95c1da3c464f3812c676ec37142b))
* **share:** record why _compact_shared reads fail-soft on purpose ([a95be19](https://github.com/bhargavamin/contexer/commit/a95be195d07a005b55ffe3a5356386e7d0fed3bb))

## [0.39.0](https://github.com/bhargavamin/contexer/compare/v0.38.0...v0.39.0) (2026-08-21)


### Features

* add decision reconciliation client ([a148c94](https://github.com/bhargavamin/contexer/commit/a148c94f11c1b794d57e8c2888fc25aff1a02c31))
* **guard:** name the staged files no armed rule could check ([a3838c2](https://github.com/bhargavamin/contexer/commit/a3838c20e3192a91912bbd9be529ec6a51f4a3a9))


### Bug Fixes

* allow explicit-team legacy reconciliation ([d3891f8](https://github.com/bhargavamin/contexer/commit/d3891f8e327751ac18e0a32c42b2b41d1caad6a9))
* consume edited reconciliation approvals ([332e113](https://github.com/bhargavamin/contexer/commit/332e1134d36cf018656a6b9c3b27c66b4f7affa8))
* **guard:** budget Tier-2 scanning by time, not by a fixed byte cap ([51ee525](https://github.com/bhargavamin/contexer/commit/51ee525eccd640de6b22d6b4d8fa62d52720eed2))
* **guard:** do not charge the scan budget for files no rule selects ([5709c21](https://github.com/bhargavamin/contexer/commit/5709c21be47da06f27ce19403b0e9dac27e11246))
* **guard:** scan staged files up to 1MB and report the ones skipped ([c6ab262](https://github.com/bhargavamin/contexer/commit/c6ab262681d7dddbdccd2662848184d0b599be66))
* **guard:** scan staged files up to 1MB and report the ones skipped ([c1024a1](https://github.com/bhargavamin/contexer/commit/c1024a1ee2445d9de4aba5e93e3a01758f0c757e))
* harden decision reconciliation retries ([8f98ee0](https://github.com/bhargavamin/contexer/commit/8f98ee03a80730dc9eb879e6b4fe6bb846c4552a))
* preserve reconciliation queue integrity ([7ba4664](https://github.com/bhargavamin/contexer/commit/7ba46640a3a2d0590ee7c2a2556df12b684eeeb6))
* remember dismissed reconciliation heads ([c16aa49](https://github.com/bhargavamin/contexer/commit/c16aa49709011ee87afe402977bdc35f86d34a99))
* serialize reconciliation outbox payloads through wire path ([92bc79a](https://github.com/bhargavamin/contexer/commit/92bc79a7f738144eda537ea83b1ad24ab6a66c89))


### Documentation

* record the guard cap fix, the scan budget, and the corrected numbers ([2e14943](https://github.com/bhargavamin/contexer/commit/2e14943808cca6354ceb0a58e39567df3236f846))
* reflow reconciliation usage sentences ([ec8bd9a](https://github.com/bhargavamin/contexer/commit/ec8bd9a8460f9709646a95cbab6f91b0200f5282))
* split reconciliation usage lines ([7e1f718](https://github.com/bhargavamin/contexer/commit/7e1f7187450d2c038301964c385cc1afe5c9a438))

## [0.38.0](https://github.com/bhargavamin/contexer/compare/v0.37.0...v0.38.0) (2026-08-19)


### Features

* **share:** push global rules to Teams with `contexer share --global` ([#239](https://github.com/bhargavamin/contexer/issues/239)) ([aa4601f](https://github.com/bhargavamin/contexer/commit/aa4601f1f0e904f2f75cb39563aec0c7dbbbf355))


### Bug Fixes

* **auth:** clear team caches on login and logout ([#232](https://github.com/bhargavamin/contexer/issues/232)) ([6b8c2af](https://github.com/bhargavamin/contexer/commit/6b8c2af42715df6de711503d7e22ab38343a2210))
* **auth:** discard the queued share outbox on login ([#232](https://github.com/bhargavamin/contexer/issues/232)) ([72b1bae](https://github.com/bhargavamin/contexer/commit/72b1baeadfd55b77a0406631795534789c118793))
* **auth:** do not let a failed outbox discard read as a cleared queue ([#232](https://github.com/bhargavamin/contexer/issues/232)) ([2fa9ec7](https://github.com/bhargavamin/contexer/commit/2fa9ec73235d1b2926b466279cd735900e86f35c))
* **auth:** serialize account-switch outbox cleanup ([6e67376](https://github.com/bhargavamin/contexer/commit/6e67376bcbed9133f926c290afc3578ff697aa42))
* **auth:** serialize login with token refresh ([b1aeb49](https://github.com/bhargavamin/contexer/commit/b1aeb49dc22e28abc75d11c1832147cfdfec76ca))
* **cli:** prefer guard hash over numeric index ([73a388e](https://github.com/bhargavamin/contexer/commit/73a388e8584782def1ddc3ae6078e82fd6e3db4f))
* **share:** gate the outbox discard on an honest read, not the fail-soft one ([#232](https://github.com/bhargavamin/contexer/issues/232)) ([9dc746f](https://github.com/bhargavamin/contexer/commit/9dc746fcbbea188660022ad671d6bd4a318439c9))
* **share:** serialize overlapping async outbox users ([e85e7aa](https://github.com/bhargavamin/contexer/commit/e85e7aab629ce246d052d9924590e1d26f8f6ec3))
* **share:** treat outbox stat failures as unsafe ([a711f3b](https://github.com/bhargavamin/contexer/commit/a711f3b9a74971d6b649da7100c9f25b6d294faa))


### Documentation

* **console:** document console_api.py and why overlap_report stayed ([c39e67d](https://github.com/bhargavamin/contexer/commit/c39e67dc991def8d002e2e1aa0fb9e6bdd5cbbb0))
* scope the adapters one-module rule to new tool targets ([#232](https://github.com/bhargavamin/contexer/issues/232), [#239](https://github.com/bhargavamin/contexer/issues/239)) ([11bc139](https://github.com/bhargavamin/contexer/commit/11bc139a95855fbab96bfd4d59907d3f312ba02f))

## [0.37.0](https://github.com/bhargavamin/contexer/compare/v0.36.1...v0.37.0) (2026-08-17)


### Features

* **share:** send source_files to contexer-teams ([cf7ec49](https://github.com/bhargavamin/contexer/commit/cf7ec49fff34781191ad6854ba44205218accc12))
* **share:** send source_files to contexer-teams ([25829c8](https://github.com/bhargavamin/contexer/commit/25829c8fd592ff3dd2249d32f9c25ff1d95963a6))


### Bug Fixes

* **share:** bound source_files at the projection too, not just the wire ([7cad60b](https://github.com/bhargavamin/contexer/commit/7cad60bd0a64a8b0e1d540eea005ff84ef4a7c31))
* **tests:** skip perf timings under coverage ([6199ef0](https://github.com/bhargavamin/contexer/commit/6199ef02b6c3c0189f111abbf294dc996e766841))

## [0.36.1](https://github.com/bhargavamin/contexer/compare/v0.36.0...v0.36.1) (2026-08-17)


### Bug Fixes

* **store:** auto-trusted constraints on plan-approval replies, dedupe title/body ([71efdf0](https://github.com/bhargavamin/contexer/commit/71efdf0736cb1f6f30cbaa564eb9e4d5d2ed89ac))
* **store:** don't auto-trust plan-approval replies, dedupe repeated title/body sentence ([19e6d34](https://github.com/bhargavamin/contexer/commit/19e6d34bbca1757654bcfb76913f68bf542b7351))
* **store:** scope title/body dedup to derived titles, not just untitled ones ([9f61b21](https://github.com/bhargavamin/contexer/commit/9f61b21f14a537e2c96f611eb84adf838b3e79a8))

## [0.36.0](https://github.com/bhargavamin/contexer/compare/v0.35.0...v0.36.0) (2026-08-16)


### Features

* **bootstrap:** point the model at repo rule docs as question evidence ([c89c404](https://github.com/bhargavamin/contexer/commit/c89c404ce87f67f6749618d882a486ba343b30ad))
* **bootstrap:** read repo rule docs as question evidence, and stop shipping junk purpose assumptions ([ff3d104](https://github.com/bhargavamin/contexer/commit/ff3d1047d3e8aae14612da7c41a888257579e920))
* **review:** remove bulk approval; make one-at-a-time review accurate ([f5aa635](https://github.com/bhargavamin/contexer/commit/f5aa635b7db14c7503d1b5c4c95c24361b9d8fab))
* **review:** remove bulk approval; make one-at-a-time review accurate ([d7c483a](https://github.com/bhargavamin/contexer/commit/d7c483a54695cd6089a1476e8e2176891bce5b40))


### Bug Fixes

* **bootstrap:** only treat a BANNER as a generated-doc marker ([b69c2f2](https://github.com/bhargavamin/contexer/commit/b69c2f2f84dec597de587e8ebf2150dbac3ba7d6))
* **bootstrap:** stop offering non-answer purpose assumptions as "Correct" ([d43563a](https://github.com/bhargavamin/contexer/commit/d43563ace0e1888db45cb693dfeac1473138cd3f))
* drop object quantifiers from the durability signal, keep recurrence only ([3e15cd7](https://github.com/bhargavamin/contexer/commit/3e15cd7314776c09325d5ec51642648fa43c832a))
* treat "ensure you"/"make sure you" as weak triggers needing a durability signal ([12d4a5b](https://github.com/bhargavamin/contexer/commit/12d4a5b80280848d48973848abc33b792c9c21d3))
* treat "ensure you"/"make sure you" as weak triggers needing a durability signal ([e5b1c0e](https://github.com/bhargavamin/contexer/commit/e5b1c0e4b743583f42fb7345eb021872e8ff0c42))


### Performance Improvements

* **review:** memoise and time-budget the review git lookups ([c0a9c0d](https://github.com/bhargavamin/contexer/commit/c0a9c0d1e1bb59a5677f993d837414f84761a5a9))


### Documentation

* **bootstrap:** document rule-doc evidence flow and the purpose fixes ([1ebf870](https://github.com/bhargavamin/contexer/commit/1ebf8702242cff6f2fda03b7a63e2eeb6795f839))

## [0.35.0](https://github.com/bhargavamin/contexer/compare/v0.34.2...v0.35.0) (2026-08-15)


### Features

* **ui:** render a rewritten proposal as a sentence-aligned side-by-side diff ([5536014](https://github.com/bhargavamin/contexer/commit/5536014f040779e864c74d96dd115656abe6f3f6))
* **ui:** render a rewritten proposal as a side-by-side diff ([1f23971](https://github.com/bhargavamin/contexer/commit/1f23971130b287787c06e53dfe96607a9c60a194))


### Bug Fixes

* **ui:** tell a list marker from a sentence that ends in a number ([8ed0bfd](https://github.com/bhargavamin/contexer/commit/8ed0bfd698bb497fc57007d786464025464feaf7))

## [0.34.2](https://github.com/bhargavamin/contexer/compare/v0.34.1...v0.34.2) (2026-08-14)


### Bug Fixes

* bound the never-mind clause strip on conjunctions, not just punctuation ([7e24d64](https://github.com/bhargavamin/contexer/commit/7e24d64865d79127d1f58f171994f05560a74f84))
* reduce false positives in the per-prompt constraint auto-capture ([a3ebede](https://github.com/bhargavamin/contexer/commit/a3ebedeccca11bc8a3ff5a11b8d0d1d024844de9))
* reduce false positives in the per-prompt constraint auto-capture ([bcdb6f6](https://github.com/bhargavamin/contexer/commit/bcdb6f69727d48130e3863118c9e0cd261a111be))

## [0.34.1](https://github.com/bhargavamin/contexer/compare/v0.34.0...v0.34.1) (2026-08-14)


### Bug Fixes

* only footer session-start title-only note when a body was clipped ([9400246](https://github.com/bhargavamin/contexer/commit/9400246cdb986a139d3735f08460b228e3476ef0))
* only footer the convention/pattern title-only note when a body was actually clipped ([46a327d](https://github.com/bhargavamin/contexer/commit/46a327dc445a1403e82709b186ae34127507397f))


### Documentation

* describe title-only convention/pattern rendering in SessionStart bullet ([250746b](https://github.com/bhargavamin/contexer/commit/250746b35209a141e2078d975cae291fde5db4ee))

## [0.34.0](https://github.com/bhargavamin/contexer/compare/v0.33.3...v0.34.0) (2026-08-14)


### Features

* **capture:** bounce a multi-section document into one decision per claim ([3e14c15](https://github.com/bhargavamin/contexer/commit/3e14c154d08e03335063bb7c84159cdf63f3e63d))
* **scope:** diagnose wrong-store writes; split multi-claim captures ([ce61c88](https://github.com/bhargavamin/contexer/commit/ce61c8888f09e396596bbec8c9c09c4bc7bdc274))
* **scope:** stamp which signal chose a decision's store, and audit for wrong ones ([4a312fc](https://github.com/bhargavamin/contexer/commit/4a312fc8d856b4bfec97b652e9e616d93237c388))


### Bug Fixes

* **retrieval:** rebuild a stale index sidecar at session start ([93bb92d](https://github.com/bhargavamin/contexer/commit/93bb92d6219109a391356fa87026fd8aaba43c26))
* **scope:** label a store whose repo directory is gone ([1717f31](https://github.com/bhargavamin/contexer/commit/1717f315ac0cc1ad5c083689c7a027e523dc8828))

## [0.33.3](https://github.com/bhargavamin/contexer/compare/v0.33.2...v0.33.3) (2026-08-13)


### Bug Fixes

* ack a refused lower-trust correction in band instead of a false success ([2f65c13](https://github.com/bhargavamin/contexer/commit/2f65c133ff43601d6d7b6507c8a5849fd6e6dacc))
* ack a refused lower-trust correction in band instead of a false success ([a3d4c72](https://github.com/bhargavamin/contexer/commit/a3d4c721b7bcbc769d5383b4747d7bdc102de2a7)), closes [#202](https://github.com/bhargavamin/contexer/issues/202)

## [0.33.2](https://github.com/bhargavamin/contexer/compare/v0.33.1...v0.33.2) (2026-08-13)


### Bug Fixes

* address code-review findings on conflict rendering and the proposal slot ([41689fd](https://github.com/bhargavamin/contexer/commit/41689fd573065e6821e51c54398862a53353fa73))
* amend pending drafts in place ([#199](https://github.com/bhargavamin/contexer/issues/199)); trust-order the proposal slot ([#200](https://github.com/bhargavamin/contexer/issues/200)) ([e4c777b](https://github.com/bhargavamin/contexer/commit/e4c777b5bf7bc257cccf26a73b3506c8c683cd56))

## [0.33.1](https://github.com/bhargavamin/contexer/compare/v0.33.0...v0.33.1) (2026-08-12)


### Bug Fixes

* **bench:** close the TOCTOU race in the --out-reuse guard ([eae1b9e](https://github.com/bhargavamin/contexer/commit/eae1b9e1bd2b9ff258ffeadd123adb95859c4e21))
* **bench:** close TOCTOU race in the memory-campaign --out-reuse guard ([15ae33e](https://github.com/bhargavamin/contexer/commit/15ae33efa80ec84e7ef96037ff7208d3291c3c79))

## [0.33.0](https://github.com/bhargavamin/contexer/compare/v0.32.0...v0.33.0) (2026-08-12)


### Features

* **bench:** capture-rate inspectors ([6680fb1](https://github.com/bhargavamin/contexer/commit/6680fb14aa10251c53079f70be5ecf6fe49d6a82))
* **bench:** frozen teaching scripts ([5ef4c4a](https://github.com/bhargavamin/contexer/commit/5ef4c4a3279f37c88edb7baa5a4046b135bb83a3))
* **bench:** memory-campaign task definitions ([d7fecbe](https://github.com/bhargavamin/contexer/commit/d7fecbe978e032f68804c2ad7eed8fca94f62c12))
* **bench:** memory-home helpers + pilot findings ([ec58a55](https://github.com/bhargavamin/contexer/commit/ec58a55e63e9f0dbeb32b4387023ab85317d7657))
* **bench:** memory-vs-contexer campaign runner ([18b23b1](https://github.com/bhargavamin/contexer/commit/18b23b1749a2b83cce6a084d17a6d64832e207eb))
* **bench:** sup-current slot scorer with pilot answer set ([ef29a84](https://github.com/bhargavamin/contexer/commit/ef29a84e33db5a0095e39b034c245412dbab136c))
* **bench:** validator isolation and tier-coverage checks ([0d8cc74](https://github.com/bhargavamin/contexer/commit/0d8cc743a753d0c475e44be966dc96586dabcfb0))
* **bench:** wilson intervals + memory-campaign report section ([49d04fc](https://github.com/bhargavamin/contexer/commit/49d04fc4a585c8ecc67482a234e8ea569ada88f9))
* **miner:** mine ruff's selected rule set as a lint convention ([f9f0d75](https://github.com/bhargavamin/contexer/commit/f9f0d75fdb7dedee65c6c44ca4f3330750107790))


### Bug Fixes

* **bench:** capture_stats derives store filename via store._slug ([b03cbfc](https://github.com/bhargavamin/contexer/commit/b03cbfc0e947ab2ad0701c64d497d8352e750259))
* **bench:** capture-rate table sources measure rows ([a415075](https://github.com/bhargavamin/contexer/commit/a4150752ffe3f79331195be11053bfe27c79914a))
* **bench:** explicit-tier rule was deictic, log regex matched "catalog" ([e0ab33d](https://github.com/bhargavamin/contexer/commit/e0ab33d829a7facb0e61843663cd8f430fa3f95b))
* **bench:** lint clean — ruff never ran locally during this build ([acdb4cc](https://github.com/bhargavamin/contexer/commit/acdb4cc4aa4d0f925f1cbe988510c71eab1dcc09))
* **bench:** memory campaign measured Contexer with its hooks deleted ([83341ad](https://github.com/bhargavamin/contexer/commit/83341ad3364c76a592703b0f47bcf09a0276c0ff))
* **bench:** pooled headline cell, review bucket, validator memory contract ([566a4a6](https://github.com/bhargavamin/contexer/commit/566a4a67000fac5cb97dcca7d2ee765fe8266757))
* **bench:** post-run contamination, per-row error capture, loud enf setup ([e528d16](https://github.com/bhargavamin/contexer/commit/e528d16e90725b781bbe3773d8ba8443250dcb91))
* **bench:** refuse to run into an --out dir that already has rows ([02311c4](https://github.com/bhargavamin/contexer/commit/02311c47f78a386f966797cfc9d8ff1c51c336f4))
* **bench:** score before check_cmd, per-session tool_calls, observed guard block ([67c79b1](https://github.com/bhargavamin/contexer/commit/67c79b1856140eded9d6f6dd52fc96fff487b6f5))
* **cli:** byte-exact guard-hook IO, an AST invariant to keep it, and ruff-gate mining ([a1f3b5f](https://github.com/bhargavamin/contexer/commit/a1f3b5fa0a70950cffbc4d544aef9c4f5d6c0988))
* **cli:** read and write the guard hook byte-exactly ([05d093b](https://github.com/bhargavamin/contexer/commit/05d093ba5c4ab4c5f1f5f679aa0b1ac4dc838bc5))
* **deps:** bump mcp, cryptography, starlette, pydantic-settings, python-multipart ([72d0b2b](https://github.com/bhargavamin/contexer/commit/72d0b2b4198899b27bfe858f4d896b7527ed7763))
* **deps:** bump mcp, cryptography, starlette, pydantic-settings, python-multipart ([f77bd10](https://github.com/bhargavamin/contexer/commit/f77bd104a44e2d2dcdde4afb325c2a50fd371dfd))
* **deps:** bump mcp, cryptography, starlette, pydantic-settings, python-multipart ([2aa0531](https://github.com/bhargavamin/contexer/commit/2aa0531b8bcbd7a3da374ca020509ce54285ce4d))
* **miner:** let an empty lint.select suppress a legacy top-level one ([00e8486](https://github.com/bhargavamin/contexer/commit/00e8486e3bfc46ccf15f5d7b6047426741eae24b))


### Documentation

* **assets:** redesign benchmark image as comparison stat cards ([79b02e5](https://github.com/bhargavamin/contexer/commit/79b02e50c7238f08c7b4a97085242221f8ed1e7c))
* **bench:** memory-campaign runbook ([06454d6](https://github.com/bhargavamin/contexer/commit/06454d6ef2e23d6ca3a642047d43db171583ef12))
* **bench:** step-2 smoke must never run at --reps 1 ([f970992](https://github.com/bhargavamin/contexer/commit/f970992740d979ad14947bdd4154fe76b5d6d208))
* **readme:** benchmark stats as a themed stat-tile image ([fa3bd6f](https://github.com/bhargavamin/contexer/commit/fa3bd6f2582f4fffae4ec5d93ea03abd778ccb8e))
* **readme:** pain-first rewrite with problem/feature matrix ([0af6b65](https://github.com/bhargavamin/contexer/commit/0af6b65260de54813ea7e5d86224f532a047d77e))
* **readme:** pain-first rewrite with problem/feature matrix ([edb2bf7](https://github.com/bhargavamin/contexer/commit/edb2bf70bfe1de9071228279f4c15b6b82d0f885))
* **readme:** surface headline benchmark numbers in a stats table ([261264f](https://github.com/bhargavamin/contexer/commit/261264fb19dacfad52331b577de1ef4a7975bba5))

## [0.32.0](https://github.com/bhargavamin/contexer/compare/v0.31.0...v0.32.0) (2026-08-09)


### Features

* **anchors:** decision-file navigation and review-gated decay ([336d2af](https://github.com/bhargavamin/contexer/commit/336d2af2a3d85f7db3896353d92e2498f6b5803a))
* **anchors:** review-gated anchor decay — rename re-anchor, retirement proposals ([df92fe6](https://github.com/bhargavamin/contexer/commit/df92fe630e3365b50105341e588a08cd74e174d5))
* **retrieval:** deterministic file-route injection at the prompt seam ([36dc13b](https://github.com/bhargavamin/contexer/commit/36dc13bcfd30c531dea7dddddcf934dca70e83c9))
* **retrieval:** get_context(files=…) — decisions that govern the given files ([2144595](https://github.com/bhargavamin/contexer/commit/21445954b01829f66781f42fbf6faa79c4252af9))
* **retrieval:** route prompt-named files through the anchor lookup ahead of BM25 ([1f99e6b](https://github.com/bhargavamin/contexer/commit/1f99e6b0a75d1d8a03952c64d15b2e71c8d14e09))
* **share:** carry source_files in projection and preview, gated off the wire pending server support ([6961049](https://github.com/bhargavamin/contexer/commit/6961049ceef22bb08313a83b65735404c24ac7c1))
* **ui:** show decision file references and filter by file ([ee43615](https://github.com/bhargavamin/contexer/commit/ee43615c105a7aa52ba7c82b0f2a0fff0bf3a9bb))


### Bug Fixes

* **anchors:** approved retirements exit participation; budget exhaustion leaves entries unverified ([37f63e7](https://github.com/bhargavamin/contexer/commit/37f63e7d974275f801c1eacb5f980a63503e40ce))
* **anchors:** follow rename chains so multi-hop renames re-anchor instead of retiring ([757eaba](https://github.com/bhargavamin/contexer/commit/757eabaa25a24b95f75767114bb347470663d1d3))
* **anchors:** retirement approvals clear candidates and preserve titles; budget guarantees progress ([c1627dc](https://github.com/bhargavamin/contexer/commit/c1627dc2729d54f9dc1e28b699ebda66bd3de5f9))
* **retrieval:** tier file-route hits by signal strength; measured serving path ([a2cda33](https://github.com/bhargavamin/contexer/commit/a2cda3315f0395b8839183c047be4171a419237e))


### Documentation

* **anchors:** document file navigation and review-gated decay ([026122c](https://github.com/bhargavamin/contexer/commit/026122c9d4d44a1abfcfb941306b97b030d05b08))
* **anchors:** honest dismiss/partial-loss/linked wording ([669c808](https://github.com/bhargavamin/contexer/commit/669c8082f1b6c36bd1af5e3fb585cc2e31012316))

## [0.31.0](https://github.com/bhargavamin/contexer/compare/v0.30.0...v0.31.0) (2026-08-08)


### Features

* **capture:** carry anchor candidates from edited files; bless at approval ([c11ecf2](https://github.com/bhargavamin/contexer/commit/c11ecf21e0f7a75c6aed266ad6d915203045e749))
* **capture:** carry anchor candidates on constraint capture ([f098213](https://github.com/bhargavamin/contexer/commit/f098213a95d48d2e02273cc9cb29d1354d4cd326))
* **capture:** record per-session edited files from write hooks ([a1ca4fa](https://github.com/bhargavamin/contexer/commit/a1ca4fac272e1868f83c4ab4ca78dc1a6afe1a9b))
* **guard:** anchor accrual — assisted backfill and capture-time candidates ([305b6e4](https://github.com/bhargavamin/contexer/commit/305b6e44c9007eaaa0140490a47811e33e9664d4))
* **guard:** anchor coverage — trust widening, approval-time anchoring, guard_engine extraction ([14cd854](https://github.com/bhargavamin/contexer/commit/14cd8548011b13b72526fbb8fbfe64906f4f7dbc))
* **guard:** anchor decisions at approval time ([21574a3](https://github.com/bhargavamin/contexer/commit/21574a3d1b1795566d742e28cd16c60187c1adcb))
* **guard:** assisted anchor backfill via contexer guard anchors ([0f9aa65](https://github.com/bhargavamin/contexer/commit/0f9aa65de889c04e297f97dfcd5f121762085fb0))
* **guard:** trust explicitly human-approved decisions regardless of capture source ([5ee2cfb](https://github.com/bhargavamin/contexer/commit/5ee2cfbcea9050d76cbbb297dd644100e9b79c6d))
* **guard:** trust explicitly human-approved decisions regardless of capture source ([36e3b5a](https://github.com/bhargavamin/contexer/commit/36e3b5a8bf13fca46fc2c36118a9bffe337a3d88))
* **guard:** trust plan-sourced approved decisions; back-stamp legacy revision sources ([ad0bfdd](https://github.com/bhargavamin/contexer/commit/ad0bfddeeb1847e518d1643fe246fa8f5d553e06))


### Bug Fixes

* **capture:** fall back to hook cwd for edit recording in non-git projects ([3fde7aa](https://github.com/bhargavamin/contexer/commit/3fde7aa78e895b8a988d287a86884ad24188308d))
* **capture:** gate anchor candidates on resulting status, pin three-way precedence ([8492874](https://github.com/bhargavamin/contexer/commit/8492874002ccd758276e501a26890d1744840d6f))
* **capture:** isolate edited-file recording from the capture-reminder arm ([a154b23](https://github.com/bhargavamin/contexer/commit/a154b23fa60bf1b02a3ce1c66baa4f961a2d8d58))
* **capture:** key edited-files sidecar per repo, bound by a freshness window ([298de03](https://github.com/bhargavamin/contexer/commit/298de035ee6076baf0a5709dcf4c15722adedfc0))
* **capture:** resolve Gemini capture and recording from the same hook cwd ([b38313e](https://github.com/bhargavamin/contexer/commit/b38313ed4457faff91388e664343dd59859b16f1))
* **guard:** abort skips backfill writes; reject unknown flags ([9539374](https://github.com/bhargavamin/contexer/commit/95393744223801d401cbf75cd5db51464db3be0f))
* **guard:** guard backfill writes against concurrent anchors; align edit-path validation ([096d92d](https://github.com/bhargavamin/contexer/commit/096d92dcfcaf5f26e6ecfae63ef79050b1564496))
* **guard:** invalidate the human-approval stamp when a non-human revision goes live ([7f8d64a](https://github.com/bhargavamin/contexer/commit/7f8d64a983e643a627e57c46c73b54bb9907f4ce))
* **guard:** make store's guard re-exports lazy so import order cannot break ([faca016](https://github.com/bhargavamin/contexer/commit/faca016be6e1630f47f3ae8ee34876cb5b528595))
* **guard:** trust legacy provenance at read time, not via storage back-stamp ([aa1a86b](https://github.com/bhargavamin/contexer/commit/aa1a86b75561c7f0fa64a73fa4c55770c31a82fe))
* **plugin:** make hook parity two-directional and cover matcher/once ([dc8ffe9](https://github.com/bhargavamin/contexer/commit/dc8ffe99b6a7191c9d2ef7bdf3de7bfa50a4e716))
* **plugin:** resync bundled hooks with adapter-generated commands; add parity test ([f80e16b](https://github.com/bhargavamin/contexer/commit/f80e16bae98bd89f19cbc848e13d7f74bf1b017d))
* **plugin:** resync bundled hooks with adapter-generated commands; add two-directional parity test ([cbd78e2](https://github.com/bhargavamin/contexer/commit/cbd78e21b3ca5ff292c6b6cf420752b5bf567a5e))
* **store:** canonicalize anchors, pair __dir__ with __getattr__, docstring + coverage debt ([f962b9b](https://github.com/bhargavamin/contexer/commit/f962b9b10a6ca089c12042e31ee7d2e32d442f50))
* **store:** drop outside-repo anchors at canonicalization ([b96db10](https://github.com/bhargavamin/contexer/commit/b96db10abfe9384eb9c9ad17b9605d59c4fea33a))
* **store:** recompute confidence after stamp invalidation, not before ([1514252](https://github.com/bhargavamin/contexer/commit/1514252e6825928d970e0011bb18f2f2812f408b))


### Documentation

* **guard:** correct tier-1 pairing claims for unanchored decisions ([3ba507a](https://github.com/bhargavamin/contexer/commit/3ba507a2c521a7ac29be73ef536a17ec0a8d10cb))
* **guard:** document anchor accrual — backfill, edited-files signal, candidate lifecycle ([7fbcb7a](https://github.com/bhargavamin/contexer/commit/7fbcb7a7f78eaa05133c2dd5d79c0595a1a5c8e5))
* **guard:** document trusted-source widening, approval anchoring, guard_engine layout ([c5681cf](https://github.com/bhargavamin/contexer/commit/c5681cfd45899043a6256d03692cec794b1fa909))
* **guard:** rewrite user-facing guard docs for clarity ([afe665b](https://github.com/bhargavamin/contexer/commit/afe665b9bf00b4c3afa25db7c5f80b353f357070))
* **guard:** scope approval-anchoring claims to paths that exist ([8bf1c79](https://github.com/bhargavamin/contexer/commit/8bf1c791a04436bf9639071ea3b64f594006cbcb))
* **store:** scope the provenance ruling — synthesis derives, never fabricates ([b37c22b](https://github.com/bhargavamin/contexer/commit/b37c22b3de1a64bf1db063b318d03832a6ef9405))
* **store:** scope the provenance ruling — synthesis derives, never fabricates ([#176](https://github.com/bhargavamin/contexer/issues/176)) ([3e925c5](https://github.com/bhargavamin/contexer/commit/3e925c58bc9854d3619304a0be54ca234961b780))

## [0.30.0](https://github.com/bhargavamin/contexer/compare/v0.29.1...v0.30.0) (2026-08-07)


### Features

* **guard:** add contexer guard CLI subcommand family ([fa6db74](https://github.com/bhargavamin/contexer/commit/fa6db746efca2ae361f356926e60002a0ae0610e))
* **guard:** add git hook install/uninstall + pre-commit framework spec ([40de902](https://github.com/bhargavamin/contexer/commit/40de9029f0558ff83be55047a32150843cdb4934))
* **guard:** add staged plumbing and path-matching helpers ([a670012](https://github.com/bhargavamin/contexer/commit/a670012acd1fbcab64d1a0ba0aae298400770a49))
* **guard:** add Tier-1 advisory engine — pairing, throttle, dismissals ([c9d75a8](https://github.com/bhargavamin/contexer/commit/c9d75a84001c7683f960d6ffc9fceae7726f5c56))
* **guard:** add Tier-2 armed rules — machine-checkable blocking checks ([918b585](https://github.com/bhargavamin/contexer/commit/918b58534e2b4acd16e5a4662cd5ba9b134b8254))
* **guard:** commit-time decision guard — advisory pairing and armed blocking rules ([67469f7](https://github.com/bhargavamin/contexer/commit/67469f73ed8a2c879e06ff86dad33fba6151fc62))
* **store:** bounce narrative-shaped AI captures with restate guidance ([02ee9ce](https://github.com/bhargavamin/contexer/commit/02ee9ceaf1d2da67734d1e7c5154628af4619755))
* **store:** clip long decision bodies in human review surfaces ([8da4a3a](https://github.com/bhargavamin/contexer/commit/8da4a3aaf713833edd12e6d667a3b451c6c48e38))
* **store:** re-verify scan conventions against fresh miner evidence ([bd5648d](https://github.com/bhargavamin/contexer/commit/bd5648dc29fd16d688dac9c60e66d29c93a638e7))
* week-1 capture quality — lint narrative captures, clip review surfaces, stamp legacy entries, re-verify scan conventions ([29d461f](https://github.com/bhargavamin/contexer/commit/29d461fbc554fb360e311b303b64400f5bd78d75))


### Bug Fixes

* **cli:** surface approve_decision failures in `contexer review` ([f0f76dd](https://github.com/bhargavamin/contexer/commit/f0f76dd62c229647e18326f8ac31b2e1c585ba18))
* **guard:** budget the whole run and pair by lookup, not scan ([833af75](https://github.com/bhargavamin/contexer/commit/833af75608e6408b1db9210a4840d70c615f2a1c))
* **guard:** probe the hook's binary and shell-quote its path ([68ee302](https://github.com/bhargavamin/contexer/commit/68ee3028b997c4b0314a99b87b0c5cb31ac6e8eb))
* **guard:** read staged paths NUL-separated so exotic names are scanned ([7e606c2](https://github.com/bhargavamin/contexer/commit/7e606c262408bdd90ebb195671d27feee152df5d))
* **guard:** report arm/disarm refusals under their own command ([420d2a2](https://github.com/bhargavamin/contexer/commit/420d2a254042695477645baa199ea912271b1a6a))
* **guard:** resolve repo once across guard CLI paths ([0f84961](https://github.com/bhargavamin/contexer/commit/0f84961fe37e4af89672760e37c19ae014efb188))
* **guard:** scan invalid-UTF-8 staged paths via surrogateescape ([5d57f6b](https://github.com/bhargavamin/contexer/commit/5d57f6b6923917d15b4198d786b1d7e899a59787))
* pin encoding="utf-8" on package text IO and guard host-config reads ([80acd21](https://github.com/bhargavamin/contexer/commit/80acd211c7323d0133b5b20e59575da764c43103))
* **store:** make disappearance proposals rule-shaped, tighten fuzzy pool ([4fe08e9](https://github.com/bhargavamin/contexer/commit/4fe08e9bbe4fb95a98d052ba18b8f5fe0fd979fb))
* **store:** pin utf-8 on the remaining reads and make .resume_mining fail-soft ([0a61370](https://github.com/bhargavamin/contexer/commit/0a6137057298e46d752a443ceb35590fa3278d7b))
* **store:** render post-verify state at session start; document capture lint in CLAUDE.md ([7ae2851](https://github.com/bhargavamin/contexer/commit/7ae2851b6ce9c632b8925a57b2ec096b2c4186cd))
* **store:** retract stale scan proposals on reappearance, tidy review pointers ([3da80db](https://github.com/bhargavamin/contexer/commit/3da80db16c11f516cafcc5659e462f9b21810e08))
* **store:** stamp missing status/created_by on legacy entries at migration ([0cac917](https://github.com/bhargavamin/contexer/commit/0cac917638935277742063ffe031509523a15dc4))
* **tests:** baseline the console's home-derived paths for the whole session ([2219c8b](https://github.com/bhargavamin/contexer/commit/2219c8b2031a93a56dfdcaf0ceec038909742217))
* **tests:** pin the real daemon test's subprocess env explicitly ([93b3398](https://github.com/bhargavamin/contexer/commit/93b3398a46d74ecfafaf59e29895b9d2fcd2c6da))


### Documentation

* document --no-cov for subset runs (coverage floor stays in addopts) ([4bcdfc7](https://github.com/bhargavamin/contexer/commit/4bcdfc712ff20c7c41155239d3232dbec8e9b9b7))
* **guard:** document the commit-time guard ([58c1e3b](https://github.com/bhargavamin/contexer/commit/58c1e3bdaf613770f0dba0a10f7c1ca0b61a1c27))
* make AGENTS.md a pointer to CLAUDE.md instead of a second copy ([dc8ccb7](https://github.com/bhargavamin/contexer/commit/dc8ccb7b87d59e8efcdfbe4b14ae38a1bd60849c))
* state why the coverage floor must stay in pytest addopts ([deb2c11](https://github.com/bhargavamin/contexer/commit/deb2c11d9c392342aaea21b721645dd6924f30fa))

## [0.29.1](https://github.com/bhargavamin/contexer/compare/v0.29.0...v0.29.1) (2026-08-05)


### Bug Fixes

* address review findings on worktree store canonicalization ([7e33fe5](https://github.com/bhargavamin/contexer/commit/7e33fe5bd98a96d5a50cf8f38d9740406591c5f0))
* attribute console-authored decisions to the developer ([c30a1fd](https://github.com/bhargavamin/contexer/commit/c30a1fd331cdeb6491738bd13d0f40fba3fc4569))
* attribute console-authored decisions to the developer, not the AI ([fbef1b4](https://github.com/bhargavamin/contexer/commit/fbef1b4494efbdb5e912c4a5acd82353153e2228))
* share the main worktree's store across linked git worktrees ([a450a23](https://github.com/bhargavamin/contexer/commit/a450a23fec8e3e236760bfa0c8743e7e771d1ddc))
* share the main worktree's store across linked git worktrees ([677e467](https://github.com/bhargavamin/contexer/commit/677e46772818122d50feef976ba9030c685de76e))
* thread created_by through update_global_decision ([a570dd2](https://github.com/bhargavamin/contexer/commit/a570dd2c064f0f40f63152f1a3bb5c859a40c686))

## [0.29.0](https://github.com/bhargavamin/contexer/compare/v0.28.0...v0.29.0) (2026-08-03)


### Features

* **auth:** report credential state and run login as a tracked job ([bc46329](https://github.com/bhargavamin/contexer/commit/bc46329e0aad9dff921efafdd5fca576859bcc86))
* **cli:** add `contexer ui`, and say why a team sync actually failed ([ef4053a](https://github.com/bhargavamin/contexer/commit/ef4053abf30b186083208d2bdeac0e7f6230ee01))
* **config:** add the [ui] settings table and an allowlisted writer ([0c61a79](https://github.com/bhargavamin/contexer/commit/0c61a795eafcce62424cb98d72ed0aa75a5fe97d))
* **store:** edit decisions, tombstone deletes, and read for a console ([75f6e21](https://github.com/bhargavamin/contexer/commit/75f6e21dbdaec7f9c2f8b5aca440135d8fa0c961))
* **ui:** add a loopback web console over the decision store ([1f1d469](https://github.com/bhargavamin/contexer/commit/1f1d46957f28b29b573131ed1c2a7de8639cad99))
* **ui:** local web console over the decision store ([301ffc9](https://github.com/bhargavamin/contexer/commit/301ffc9ca54053632743d292a1008a58661cdd8d))


### Bug Fixes

* **team-context:** record an auth rejection distinctly from an outage ([256bab3](https://github.com/bhargavamin/contexer/commit/256bab3422aca01e2a01b326cebaaf1ca7b8f763))
* **ui:** address the five console review findings ([80c8e1a](https://github.com/bhargavamin/contexer/commit/80c8e1a10aa36c5c7efe54d93afb36ff434c56f4))
* **ui:** address the five console review findings ([446fc0a](https://github.com/bhargavamin/contexer/commit/446fc0a5f7cfccca6adb07c9347bcba13966123c))
* **ui:** bring the console back onto the Contexer design system ([f0dc236](https://github.com/bhargavamin/contexer/commit/f0dc236a5fc6684af3380737b0b852e86409291d))
* **ui:** bring the console back onto the Contexer design system ([1769b7a](https://github.com/bhargavamin/contexer/commit/1769b7a44e68c402d547ba04e9d52050b44917af))


### Documentation

* document the console, and correct the module map ([b22c3ba](https://github.com/bhargavamin/contexer/commit/b22c3bafbc025e6e7c827e1f5ebd2036b639115b))

## [0.28.0](https://github.com/bhargavamin/contexer/compare/v0.27.0...v0.28.0) (2026-08-01)


### Features

* make Contexer effective for comprehension questions (capture, staleness, question-lane retrieval) ([6fc8f59](https://github.com/bhargavamin/contexer/commit/6fc8f59fea48d9bd32ca08d35eea570aa775e523))
* **store:** add bare topic names to their own alias sets for WEAK-pointer coverage ([f9df2d2](https://github.com/bhargavamin/contexer/commit/f9df2d2a8e10b3d02ef2659d56b59c51f3757537))
* **store:** flag stored summaries as stale when their source files change ([99d1b97](https://github.com/bhargavamin/contexer/commit/99d1b9726a06bfe3c1f9a3e1ce7b6f7dfda551cb))
* **store:** route question-shaped prompts, guarded by term rarity ([5c11da0](https://github.com/bhargavamin/contexer/commit/5c11da06a0d17021d851610952bf9899b4c6deb6))


### Bug Fixes

* **store:** defer replace_id re-anchoring until content is actually live ([0e7ba3b](https://github.com/bhargavamin/contexer/commit/0e7ba3bda2f0572dccdd5f58b16980ed089ee1db))
* **store:** diff staleness anchors against the working tree, not HEAD ([a3878dd](https://github.com/bhargavamin/contexer/commit/a3878dd357939ed69bb66e28dcc666658d8ace18))
* **store:** re-anchor identical-content title-only replace_id corrections ([e575f1f](https://github.com/bhargavamin/contexer/commit/e575f1f444977bdc185baff1355760914d40a412))
* **store:** re-anchor replace_id corrections and tighten benchmark/docs ([0d6a03a](https://github.com/bhargavamin/contexer/commit/0d6a03ac419dc3ef867b5b8c7b00617a2e6863ee))
* **test:** restore deploy-migrations fixture and pin its pointer honestly ([eb3cc02](https://github.com/bhargavamin/contexer/commit/eb3cc02e3c6a17ef1dd067063d1b8326b809868f))


### Documentation

* **server:** capture synthesized subsystem understanding, same-turn ([a1c2411](https://github.com/bhargavamin/contexer/commit/a1c2411d0898ba89da517fc26463c16f81afda35))

## [0.27.0](https://github.com/bhargavamin/contexer/compare/v0.26.1...v0.27.0) (2026-07-30)


### Features

* **bootstrap:** interactive picker setup offer, once per session ([539c7f7](https://github.com/bhargavamin/contexer/commit/539c7f7c6a1002b69e8f31c76251be3221af5086))
* **bootstrap:** offer setup as an interactive picker, once per session ([87f1cc1](https://github.com/bhargavamin/contexer/commit/87f1cc1f3e0cd594acfefa81758e322c72c4369a))
* **share:** page picker at 10, accept ranges like 1-4 ([ab52745](https://github.com/bhargavamin/contexer/commit/ab527459d6405fcbf3f300a9b4943e00a3fd65ed))
* **share:** page picker at 10, accept ranges like 1-4 ([3ed18ab](https://github.com/bhargavamin/contexer/commit/3ed18abbec6b9f9b9a64214af1e8a1a70657c12d))


### Bug Fixes

* **deps:** cap mcp&lt;2 — 2.0.0 removed mcp.server.fastmcp ([f4e5b58](https://github.com/bhargavamin/contexer/commit/f4e5b585e19e2206c903d34ec2491c94a8046223))
* **hooks:** never abort a hook on an unwritable ~/.contexer ([0d95fb0](https://github.com/bhargavamin/contexer/commit/0d95fb078d029c729b634078d51ad813f21893ec))
* **hooks:** never abort a hook on an unwritable ~/.contexer ([8d30104](https://github.com/bhargavamin/contexer/commit/8d3010405f75ce0ab36e27d50a1de421cbf1edbc))


### Documentation

* **bootstrap:** describe the picker, scan's real question count ([66e3e64](https://github.com/bhargavamin/contexer/commit/66e3e64b3d7223aa3a17be86fb4b4cc52136babb))

## [0.26.1](https://github.com/bhargavamin/contexer/compare/v0.26.0...v0.26.1) (2026-07-26)


### Bug Fixes

* **team-context:** address Greptile review — cap bypass, stale cache, snapshot race ([20924af](https://github.com/bhargavamin/contexer/commit/20924afe57738b8f8f2d2c0788739f3614ce0b99))
* **team-context:** preserve ratification signal under architecture deferral ([25f413f](https://github.com/bhargavamin/contexer/commit/25f413fb8632218adbb11d7a81db4e1ee0ee76d3))
* **team-context:** preserve ratification signal under architecture deferral ([0b3d816](https://github.com/bhargavamin/contexer/commit/0b3d816798920c06c9acd1bbf19cd0134a60a6c5))

## [0.26.0](https://github.com/bhargavamin/contexer/compare/v0.25.0...v0.26.0) (2026-07-25)


### Features

* send decision titles to the cloud + rework the share picker ([b27cada](https://github.com/bhargavamin/contexer/commit/b27cadae3f0be0e35904adcc743b1332a4115d0f))
* send decision titles to the cloud and rework the share picker ([dbb36e7](https://github.com/bhargavamin/contexer/commit/dbb36e74bd69d8273f7b28078ec8b15616c880d8))


### Bug Fixes

* lock the shared-marker write; match queries against the title ([122533e](https://github.com/bhargavamin/contexer/commit/122533e3d0397348b92bb417b1c179138a64991c))
* **share:** exclude marker appends during shared-log compaction ([719451f](https://github.com/bhargavamin/contexer/commit/719451fc9ad4adc1eb5326c1257ba5b61ccafb0c))
* **share:** make shared markers append-only so no platform loses updates ([c4c091c](https://github.com/bhargavamin/contexer/commit/c4c091c21e50742e9d6950c45a879479f47f5ae1))

## [0.25.0](https://github.com/bhargavamin/contexer/compare/v0.24.0...v0.25.0) (2026-07-24)


### Features

* **cli:** review shows the decision title as the headline ([98f755e](https://github.com/bhargavamin/contexer/commit/98f755e9bc947f8d9535b68e52232b543c1ee6dc))
* decision titles (v1, OSS-local) ([4552192](https://github.com/bhargavamin/contexer/commit/4552192e0cae94f1ce55c7f0e29e46068b968bb3))
* **mcp:** update_context/update_global_context accept optional title ([1171550](https://github.com/bhargavamin/contexer/commit/1171550f65c12ebb9d8657085ace40a3fa28ec14))
* **store:** add title helpers (normalize + derive, cap 100) ([bdc3665](https://github.com/bhargavamin/contexer/commit/bdc366595212078e44701daaabe6bd2c7ac1551a))
* **store:** lazy title backfill for legacy entries; bump schema to v3 ([1bab9ab](https://github.com/bhargavamin/contexer/commit/1bab9ab7fb65ca6389c1db3b6603b5c92428f749))
* **store:** leading-heading display (title line + content) in get_context ([5174597](https://github.com/bhargavamin/contexer/commit/5174597e7cb8d35ba7f5a2bc71ccbfc8abf19cc8))
* **store:** plumb title through update_decision + revisions (re-derive on omit) ([7727fc4](https://github.com/bhargavamin/contexer/commit/7727fc4eaacb09fe4e82c438fbf58dd44e3b7679))
* **store:** title field on decision entry + revision, synced to HEAD cache ([6f56a43](https://github.com/bhargavamin/contexer/commit/6f56a434baddd2bd837e53a8b201f994644cc51c))


### Bug Fixes

* **mcp:** direct the agent to author decision titles at capture ([8db362a](https://github.com/bhargavamin/contexer/commit/8db362a0547742e8cce7113230153306b8ea8643))
* **store:** carry title through the approval-gated proposal path ([1a44b7c](https://github.com/bhargavamin/contexer/commit/1a44b7cd7d23e343e98040d5c332c7e99a77000f))
* **store:** gate AI title-only changes to trusted decisions (review P1) ([ed12439](https://github.com/bhargavamin/contexer/commit/ed12439e41e13f76c21e248402d88e9d3a016b33))
* **store:** persist title-only corrections on the replace_id no-op path ([90444dc](https://github.com/bhargavamin/contexer/commit/90444dc29bbda5127af082f3572142b2526d88a5))
* **store:** title corrections no longer dropped for pending/proposal states ([7d1b902](https://github.com/bhargavamin/contexer/commit/7d1b9021c4d6b3f651084337fc7a61c4773c00f3))
* **store:** titles in the indexed prompt-injection formatter (_render_prompt_decisions) ([6da6296](https://github.com/bhargavamin/contexer/commit/6da6296814f32b525558bcbe5c257784e7245d76))

## [0.24.0](https://github.com/bhargavamin/contexer/compare/v0.23.1...v0.24.0) (2026-07-20)


### Features

* **redact:** scrub secrets on egress to the remote MCP ([ff0163a](https://github.com/bhargavamin/contexer/commit/ff0163a5afdfb9fb188ac3be6a4cafaf18435525))
* **redact:** scrub secrets on egress to the remote MCP ([63d6566](https://github.com/bhargavamin/contexer/commit/63d65662379d9c8afb9dcf7ec1d9b37be3be26bf))
* **remote:** batch push_decisions client method ([3263ecf](https://github.com/bhargavamin/contexer/commit/3263ecf8f44e815ffaf1e9ff565f077995692e9c))
* **share:** batch share --all via push_decisions; report unknown ids ([988b870](https://github.com/bhargavamin/contexer/commit/988b870118eec5e382e8907197c057fa0bfd2da6))
* **share:** batch share --all/share_ids/drain via push_decisions ([0d90c38](https://github.com/bhargavamin/contexer/commit/0d90c383a3fa19f4e4f215fc1462d24a3d08ac69))


### Bug Fixes

* **redact:** address Greptile P1s — lowercase Bearer, quoted spans, store profile ([a1c740f](https://github.com/bhargavamin/contexer/commit/a1c740fc0c86327c87bf24a532ce15eceebc3f15))
* **share:** classify batch skips - drop invalid rows, keep capacity queued ([be8d65e](https://github.com/bhargavamin/contexer/commit/be8d65e4ae84cdfc24fdc63c3bde0edf9ab9b5c7))
* **share:** honest degrade message - surface the server's reason, not 'unreachable' ([1a52037](https://github.com/bhargavamin/contexer/commit/1a520379084f1f895fe579c7f039491e1eb1f01a))

## [0.23.1](https://github.com/bhargavamin/contexer/compare/v0.23.0...v0.23.1) (2026-07-18)


### Bug Fixes

* **#108:** address Greptile P1s — enqueue on cancel; refresh off the loop ([27c0dab](https://github.com/bhargavamin/contexer/commit/27c0dab6b33f18a2dca876da8fc151a4341546ce))
* **#108:** async-native RemoteStore push path so a wedged share is cancellable ([e2fe0f6](https://github.com/bhargavamin/contexer/commit/e2fe0f6f354aade028b2dcb6f159963e66c68d6c))
* **#108:** async-native RemoteStore push path so a wedged share is cancellable ([a15d76e](https://github.com/bhargavamin/contexer/commit/a15d76ed33f02fdda38a3d73ce21bf2c70169e1e))
* **login:** bound post-login sync + refresh the repo status shows ([5df4fcd](https://github.com/bhargavamin/contexer/commit/5df4fcd57e239400e53f8abbeb79cc9505eafe11))
* **login:** pull team context after login so status isn't stale ([3311db6](https://github.com/bhargavamin/contexer/commit/3311db6936ead547991ddc79803310317456b8d0))

## [0.23.0](https://github.com/bhargavamin/contexer/compare/v0.22.0...v0.23.0) (2026-07-16)


### Features

* **claude:** recall notice shows benchmark-derived tokens saved ([6f663a2](https://github.com/bhargavamin/contexer/commit/6f663a2665c8e6af6a836386fc5cab7c6615957e))


### Bug Fixes

* **share:** normalize decision source onto the cloud's accepted taxonomy ([d949687](https://github.com/bhargavamin/contexer/commit/d94968729741f83450b80d21c123ca462131673d))
* **share:** preserve plan provenance on the cloud sync wire ([bc867e9](https://github.com/bhargavamin/contexer/commit/bc867e9518df68a2698547468be80f8d592d8692))


### Documentation

* record tokens-saved recall notice and golden multiplier derivation ([00e514c](https://github.com/bhargavamin/contexer/commit/00e514cce30860557c35cf51168d21aab4b09292))

## [0.22.0](https://github.com/bhargavamin/contexer/compare/v0.21.0...v0.22.0) (2026-07-15)


### Features

* **review:** surface possibly-overlapping rules for manual consolidation ([1648341](https://github.com/bhargavamin/contexer/commit/16483411c74533dbd713dc68288ba3c06745b73b))
* **review:** surface possibly-overlapping rules for manual consolidation ([e339031](https://github.com/bhargavamin/contexer/commit/e33903132f23be0c864adb45ae5807ecbd86a30d))


### Bug Fixes

* **capture:** best-match containment routing; never clobber a pending proposal ([e193bbe](https://github.com/bhargavamin/contexer/commit/e193bbe2ca233cb97f24a31b3189c7d04bf3e412))
* **capture:** containment-aware routing + near-miss consolidation nudge ([a6f6a9c](https://github.com/bhargavamin/contexer/commit/a6f6a9c23ddd5980d45bd6fe7f3499c81b915641))
* **capture:** containment-aware routing + near-miss consolidation nudge ([6fbb211](https://github.com/bhargavamin/contexer/commit/6fbb21186661df392d24dc59678c92cbcf157938))
* **review:** retiring approved rules is now a first-class act ([1a4ec76](https://github.com/bhargavamin/contexer/commit/1a4ec76e36e06311aeda32d80e30c103e7bb8f86))

## [0.21.0](https://github.com/bhargavamin/contexer/compare/v0.20.1...v0.21.0) (2026-07-15)


### Features

* **capture:** deictic directives stored pending review, not trusted ([1281617](https://github.com/bhargavamin/contexer/commit/1281617cb519489dbcfaea754512e67d438dda1a))

## [0.20.1](https://github.com/bhargavamin/contexer/compare/v0.20.0...v0.20.1) (2026-07-15)


### Bug Fixes

* **bench:** golden-repo copy no longer races background git gc ([5b93235](https://github.com/bhargavamin/contexer/commit/5b9323518af4c53b23ac3e80d9b750d95f3614b9))
* **bench:** golden-repo copy no longer races background git gc ([e104110](https://github.com/bhargavamin/contexer/commit/e1041106eadf55c2aa15fc663ba69607ef1586d9))
* **retrieval:** compound auth-session phrases keep the auth topic ([e6ccea8](https://github.com/bhargavamin/contexer/commit/e6ccea86512718122deb9fa9e205651db139dacf))
* **retrieval:** drop session/sessions from the auth topic aliases ([af50fbb](https://github.com/bhargavamin/contexer/commit/af50fbb982d8a4ec8799af524c77175840945d98))
* **retrieval:** drop session/sessions from the auth topic aliases ([1b803a6](https://github.com/bhargavamin/contexer/commit/1b803a62578115764222a175559141e5153b80b0))


### Documentation

* add always-available GitHub stars badge ([be63335](https://github.com/bhargavamin/contexer/commit/be63335196213a356cd1eae087f12f39ba884e28))
* add always-available GitHub stars badge ([eb36e92](https://github.com/bhargavamin/contexer/commit/eb36e92dbd8d96ed50ef7df538816358843a8ddf))
* **bench:** address Greptile [#122](https://github.com/bhargavamin/contexer/issues/122) — reproducible session count, A/B attribution scope, source provenance ([d0c6c51](https://github.com/bhargavamin/contexer/commit/d0c6c515f66a00b1ac6becfbe1e1fe89c3038ea5))
* **bench:** measured v0.20.0 overhead + paraphrase stability (campaigns 7-8) ([1c3de73](https://github.com/bhargavamin/contexer/commit/1c3de739e75f299d11fc222422784624d9f9b314))
* **bench:** measured v0.20.0 overhead + paraphrase stability (campaigns 7-8) ([80b79e8](https://github.com/bhargavamin/contexer/commit/80b79e89672e7b6e21a877d14d2a80a236d53a56))
* name every agent's rules-file equivalent, not just CLAUDE.md ([67695d8](https://github.com/bhargavamin/contexer/commit/67695d81a9ba58e588eb4362a7b5743970c0d2d4))
* name every agent's rules-file equivalent, not just CLAUDE.md ([98fc1ee](https://github.com/bhargavamin/contexer/commit/98fc1ee409c5e239e7014f023ac386f6a99ededd))
* remove Star history section ([aad5976](https://github.com/bhargavamin/contexer/commit/aad5976f70e03c1439e8ca87b709b104e4591de8))
* remove the Star history section entirely ([249fe72](https://github.com/bhargavamin/contexer/commit/249fe724748879dcce21fcb3ce142df13d78d26e))
* Star history section can no longer render a broken image ([16d6198](https://github.com/bhargavamin/contexer/commit/16d61988d604ef9212bd21b29a92ceb5f441a913))

## [0.20.0](https://github.com/bhargavamin/contexer/compare/v0.19.0...v0.20.0) (2026-07-15)


### Features

* **bench:** per-condition contexer source for version A/B campaigns ([d098db9](https://github.com/bhargavamin/contexer/commit/d098db95805ddef45e2336a886c334afc8b1ec4a))
* **retrieval:** make injections observable — user-facing cost line, no-refetch cue ([a689a94](https://github.com/bhargavamin/contexer/commit/a689a94d01a2cd2e376540df2356aa011582e6a2))
* **retrieval:** session integration — standing map, compact rehydration, working-set GC ([c50ce12](https://github.com/bhargavamin/contexer/commit/c50ce12bb0cd69a035a11a526605909ecb76a527))
* **retrieval:** status line names what was recalled; token cost flagged on exception only ([29f1bda](https://github.com/bhargavamin/contexer/commit/29f1bdad67c14ea006368a6e3fc9c9207b31d628))
* **retrieval:** topic router — BM25 index sidecar, injection ladder, working set ([879d66d](https://github.com/bhargavamin/contexer/commit/879d66db0e33101dcef1532f16ace95afaf6dddd))
* **retrieval:** topic-aware retrieval V1 — BM25 router, injection ladder, session rehydration ([e2cb022](https://github.com/bhargavamin/contexer/commit/e2cb022a1786c0fe9a017571d9866eeaafc61389))


### Bug Fixes

* **retrieval:** adapter parity — Codex/Gemini rehydration, structured status line, docs ([3496483](https://github.com/bhargavamin/contexer/commit/34964832d1caee8c643539fcb5f68be6fda4c3b6))
* **retrieval:** address Greptile [#117](https://github.com/bhargavamin/contexer/issues/117) — pending decisions indexed, ws ids hashed, CLI validation ([283483b](https://github.com/bhargavamin/contexer/commit/283483b281f73ef68c26fa546d9fb6e463295da3))
* **retrieval:** review findings — index must never lose to its own fallback ([86e21ff](https://github.com/bhargavamin/contexer/commit/86e21ff61e7af85e37a8c094a75d5f1912acd48c))


### Documentation

* README as two-audience pitch; benchmark rewritten plain-language with version-tagged results ([bbb4c24](https://github.com/bhargavamin/contexer/commit/bbb4c2456378da94e2e3c4251c1bd7c4b2cc951d))

## [0.19.0](https://github.com/bhargavamin/contexer/compare/v0.18.0...v0.19.0) (2026-07-13)


### Features

* **bench:** A/B benchmark harness, findings doc, and campaign data ([eb50c15](https://github.com/bhargavamin/contexer/commit/eb50c151da42be666340cbb7d84f74ed42f23d05))
* **bootstrap:** deterministic convention mining with evidence-tiered trust ([4bc136a](https://github.com/bhargavamin/contexer/commit/4bc136a4fafe4dbb666d6f1f711ea7d92ff4e529))
* **bootstrap:** deterministic convention mining with evidence-tiered trust ([21c501e](https://github.com/bhargavamin/contexer/commit/21c501e33021d4239bd358c8f73895e20ebbcc78))


### Bug Fixes

* **bench:** address Greptile review on [#116](https://github.com/bhargavamin/contexer/issues/116) ([71a1fb6](https://github.com/bhargavamin/contexer/commit/71a1fb652d8e4ee4d0967a1ea08cd88ce0a81ceb))
* **bench:** snapshot worktree between chain steps (Greptile [#116](https://github.com/bhargavamin/contexer/issues/116) P1) ([cc34237](https://github.com/bhargavamin/contexer/commit/cc34237fdd73f12d4dcee465efef08d6deaed026))
* **bootstrap:** address Greptile review on [#114](https://github.com/bhargavamin/contexer/issues/114) ([966e512](https://github.com/bhargavamin/contexer/commit/966e5129b149c959dd234e5b791e9623f7dfa0ff))


### Documentation

* benchmark highlights and link at top of README ([790e1a8](https://github.com/bhargavamin/contexer/commit/790e1a8eb32d81c74359787837f939bc84789148))

## [0.18.0](https://github.com/bhargavamin/contexer/compare/v0.17.2...v0.18.0) (2026-07-12)


### Features

* **review:** bulk approve + escalating backlog nudge (Phase 2, increment 1) ([ff24990](https://github.com/bhargavamin/contexer/commit/ff2499016bab1991295110d4522c653d6d9dcf38))
* **review:** deterministic .pending_review mid-session nudge ([#112](https://github.com/bhargavamin/contexer/issues/112)) ([905ce06](https://github.com/bhargavamin/contexer/commit/905ce065c03b72b48ff27fd8dbeb3fcb482d82c1))
* **review:** deterministic .pending_review mid-session nudge (issue [#112](https://github.com/bhargavamin/contexer/issues/112)) ([60918c8](https://github.com/bhargavamin/contexer/commit/60918c8e5f88b57f83368ba85fca80cf96a3e249))
* **review:** non-blocking pending-review UX + confirm-before-push ([6cdefc0](https://github.com/bhargavamin/contexer/commit/6cdefc02246abef57394582d8a4579ad133ae3ac))
* **review:** non-blocking pending-review UX + confirm-before-push ([5cdc33a](https://github.com/bhargavamin/contexer/commit/5cdc33ad08bd96eb35e221f6d0325ccb74f4758e))
* **review:** Phase 2 — bulk approve + backlog escalation (anti-pile-up) ([0c51afd](https://github.com/bhargavamin/contexer/commit/0c51afd6d121e33a3d7cee6a3374b4e848295522))
* **share:** pick-and-multi-select instead of guessing a decision id ([aa31189](https://github.com/bhargavamin/contexer/commit/aa311899825f803449b71dd0efd7b3da226dd23c))


### Bug Fixes

* **review,share:** resolve id prefixes on approve; gate preview on auth; stabilize latency bench ([8d9d03c](https://github.com/bhargavamin/contexer/commit/8d9d03c24aff297841bf58fb0794cb954dc121d7))
* **review:** per-repo + verified pending-review nudge (Greptile [#113](https://github.com/bhargavamin/contexer/issues/113)) ([25166bc](https://github.com/bhargavamin/contexer/commit/25166bc745aced146ba14bf80f557ddc0788a431))
* **review:** safe + atomic bulk approve (code-review + Greptile) ([9ba08af](https://github.com/bhargavamin/contexer/commit/9ba08af145157782929263a8217d758f5d296291))

## [0.17.2](https://github.com/bhargavamin/contexer/compare/v0.17.1...v0.17.2) (2026-07-11)


### Bug Fixes

* **server:** bound share_decision's worker wait with a timeout backstop ([5e5b493](https://github.com/bhargavamin/contexer/commit/5e5b493a53fa9f88a42f74c7f5b03492075a7219))
* **server:** make share_decision async so it never blocks the event loop ([d5d4809](https://github.com/bhargavamin/contexer/commit/d5d4809bafce080f5e60a586b3d75e35f223a384))
* **server:** make share_decision async so MCP shares don't freeze/fail on the event loop ([08d813d](https://github.com/bhargavamin/contexer/commit/08d813d03ed5c355c76391d5c258d2cf05a9c630))

## [0.17.1](https://github.com/bhargavamin/contexer/compare/v0.17.0...v0.17.1) (2026-07-10)


### Bug Fixes

* **auth:** don't refresh the single-use token when serialization is unavailable ([31e2f06](https://github.com/bhargavamin/contexer/commit/31e2f066dcf8e39a47b6c4e5f8269cc1a467546e))
* **auth:** serialize OAuth token refresh to stop single-use refresh-token double-spend ([9efd596](https://github.com/bhargavamin/contexer/commit/9efd596d859df7d5d2ee374dd0e951ec36325fe1))
* **auth:** serialize OAuth token refresh to stop single-use refresh-token double-spend ([81f436a](https://github.com/bhargavamin/contexer/commit/81f436aa9bcf1151a7ca8c2056e9c65b2a931af9))
* stop uv.lock revision flapping between 2 and 3 ([3bcec2d](https://github.com/bhargavamin/contexer/commit/3bcec2dc08c9c32e76e75b7725b43218ccb360cb))
* stop uv.lock revision flapping between 2 and 3 ([aa810c2](https://github.com/bhargavamin/contexer/commit/aa810c2891f3d45033d056d2ff72dce3b6b1f306))

## [0.17.0](https://github.com/bhargavamin/contexer/compare/v0.16.1...v0.17.0) (2026-07-07)


### Features

* **share:** add contexer share --all to push every decision ([5c02849](https://github.com/bhargavamin/contexer/commit/5c02849b90e26040c8b4a0312051dfe77b8a1403))


### Bug Fixes

* **hooks:** resolve non-git project dirs via the hook's own cwd ([d28cb7a](https://github.com/bhargavamin/contexer/commit/d28cb7a3f5d38933ef49e991a64edfa49814b3cf))
* **hooks:** resolve non-git project dirs via the hook's own cwd ([cefa543](https://github.com/bhargavamin/contexer/commit/cefa5431d6656859623c88c1b8f834f1f227c243))
* **share:** report exact queued count when the outbox write fails mid-queue ([5c487ef](https://github.com/bhargavamin/contexer/commit/5c487efd9e4c0c56b6ba8761315659613f7ca9c9))
* **store:** guard _hook_cwd_repo against a deleted working directory ([323828d](https://github.com/bhargavamin/contexer/commit/323828d01f4e0136de820507667c40511580a924))

## [0.16.1](https://github.com/bhargavamin/contexer/compare/v0.16.0...v0.16.1) (2026-07-07)


### Bug Fixes

* **adapters:** drop empty hooks key on retire; move cursor stub below markers ([1336b35](https://github.com/bhargavamin/contexer/commit/1336b35a2c8c12e6293c6eced158bb24b9c83ba8))
* **adapters:** self-retiring stubs for removed capture_task hook entrypoints ([f14c29e](https://github.com/bhargavamin/contexer/commit/f14c29e213823613e8fb91eaa383f7d5aab22e75))
* **adapters:** self-retiring stubs for removed capture_task hook entrypoints ([fc52b1c](https://github.com/bhargavamin/contexer/commit/fc52b1c04b76c098dda9b18f928525814fb6c44a))
* **claude:** clean legacy pre-CLI hooks left behind by upgrades ([830d816](https://github.com/bhargavamin/contexer/commit/830d816a431f37e68774d74513081efe3277a528))
* **claude:** clean legacy pre-CLI hooks left behind by upgrades ([ecd7d22](https://github.com/bhargavamin/contexer/commit/ecd7d2285d44c1cf5120068bf5520e1f10e8fe31))
* **claude:** refuse home dir in legacy repo-settings cleanup ([e3379be](https://github.com/bhargavamin/contexer/commit/e3379bed5a1f0bfe247aa955809b313ad5a1e62b))

## [0.16.0](https://github.com/bhargavamin/contexer/compare/v0.15.0...v0.16.0) (2026-07-06)


### Features

* **cli:** surface team sync visibility in status, session start, and share ([852d737](https://github.com/bhargavamin/contexer/commit/852d737cf2f164fff918cc4641f85561da45bee1))
* **share:** durable outbox with session-start drain ([ca5ac2a](https://github.com/bhargavamin/contexer/commit/ca5ac2ae33f742eff494eea03c2e07a46e89eda5))
* **team-sync:** bound session-start pull timeout to 3s ([e792219](https://github.com/bhargavamin/contexer/commit/e7922195d27ac87395e11799c22a24db6739fe22))
* **team-sync:** collapse team rows that duplicate local decisions ([70f3291](https://github.com/bhargavamin/contexer/commit/70f32911df682c94461d6f5523ebfbf37006bd63))
* **team-sync:** record last_sync telemetry on every attempted sync ([f05bd7b](https://github.com/bhargavamin/contexer/commit/f05bd7ba66f37e0b6a23124376bcc64f84c07ca4))
* **team-sync:** record render size for future display-cap tuning ([3db2142](https://github.com/bhargavamin/contexer/commit/3db2142de300c52a68f7dabaacb08f87062f8256))
* **team-sync:** staleness tag + exponential failure backoff ([ee9d138](https://github.com/bhargavamin/contexer/commit/ee9d1380915697a439e1469066b024a31cdbd36c))


### Bug Fixes

* **outbox:** close TOCTOU window in drain_outbox final save ([7803625](https://github.com/bhargavamin/contexer/commit/7803625f32baf9569db58dab464cf3f4f71a55eb))
* **outbox:** drain failure must not block the current share ([4a5fcd8](https://github.com/bhargavamin/contexer/commit/4a5fcd878a038549419d1709245b9f0907dcee21))
* **outbox:** enqueue failure must not escape share() ([d7b312a](https://github.com/bhargavamin/contexer/commit/d7b312a9c96bd827d4a7eb0f7feb06dd147917bc))
* **remote:** classify insufficient-scope tool errors as auth failures ([8190ba6](https://github.com/bhargavamin/contexer/commit/8190ba6c9242c46fc06d5831e5c7c3fdc08b080a))
* **remote:** phrase-level auth-error matching + honest tool-error degradation message ([4082cf1](https://github.com/bhargavamin/contexer/commit/4082cf127cdc20340d1a92b29d52f55cd048c132))
* **team-context:** preserve non-team scope in partial-overlap branch ([4ad65a6](https://github.com/bhargavamin/contexer/commit/4ad65a6793d892fb738eba1731e4ed245e856e1c))
* **team-poll:** guard legacy-pending unlink and harden codex migration marker ([eacf882](https://github.com/bhargavamin/contexer/commit/eacf8821267cabbfb8236d4cdcaab3a63f90ac6a))
* **team-sync:** per-consumer team-poll delivery via sync log + high-water marks ([734704a](https://github.com/bhargavamin/contexer/commit/734704a863832f0389b9b5e534bb18ac6d3f5861))
* **team-sync:** record render telemetry against a fresh cache snapshot ([570e03e](https://github.com/bhargavamin/contexer/commit/570e03ecc70646d493aaf07807572cb30883bf70))
* **team-sync:** review fixes - cap-aware status count, sync-start timestamp, de-flaked test ([626f05a](https://github.com/bhargavamin/contexer/commit/626f05aaa9ee92ccedbd2edd87eeee7338382be4))

## [0.15.0](https://github.com/bhargavamin/contexer/compare/v0.14.2...v0.15.0) (2026-07-05)


### Features

* **team-sync:** adapter-agnostic team-context seam (T1) ([0f99b4d](https://github.com/bhargavamin/contexer/commit/0f99b4d7e43d2b1b04760eff9ab49cf5c04739eb))
* **team-sync:** adapter-agnostic team-context seam (T1) ([f395f5c](https://github.com/bhargavamin/contexer/commit/f395f5c8dbc479f57dddeab9a05cb9144df26f62))
* **team-sync:** Codex SessionStart pull + per-prompt delta poll (T2) ([74be5f6](https://github.com/bhargavamin/contexer/commit/74be5f602ad0717e0029b6935f8dc0d7edb9c481))
* **team-sync:** Codex SessionStart pull + per-prompt delta poll (T2) ([1a4c195](https://github.com/bhargavamin/contexer/commit/1a4c19514c5466f4868c92fe3a8614dace1c4ea1))


### Bug Fixes

* **team-sync:** restore pull_team import fail-soft + suppress team on resume ([a7e7ba1](https://github.com/bhargavamin/contexer/commit/a7e7ba13d224901622d91ce82590049e6a14af97))

## [0.14.2](https://github.com/bhargavamin/contexer/compare/v0.14.1...v0.14.2) (2026-07-04)


### Bug Fixes

* **bootstrap:** answer a repo question instead of showing the setup menu ([8307387](https://github.com/bhargavamin/contexer/commit/8307387b9117eeab47012a7fc4cac19c4dc9ba36))
* **bootstrap:** answer a repo question instead of showing the setup menu ([4751ce9](https://github.com/bhargavamin/contexer/commit/4751ce9ee98c09c66b278c48332ed1b556c2939f))

## [0.14.1](https://github.com/bhargavamin/contexer/compare/v0.14.0...v0.14.1) (2026-07-04)


### Bug Fixes

* resolve SSH host aliases when deriving the canonical repo key (C2) ([3afe686](https://github.com/bhargavamin/contexer/commit/3afe68657e92166d086b856fecf4f2d736661d0e))
* resolve SSH host aliases when deriving the canonical repo key (C2) ([0459c2b](https://github.com/bhargavamin/contexer/commit/0459c2bf83f724abb8d09bc2b6bc5da7821b4ceb))

## [0.14.0](https://github.com/bhargavamin/contexer/compare/v0.13.0...v0.14.0) (2026-07-04)


### Features

* add contexer login (zero-paste OAuth) (C-auth) ([1e9a840](https://github.com/bhargavamin/contexer/commit/1e9a840e2e3a8d12ddfa8f97dffc4ecc3c97d9f3))
* add contexer login (zero-paste OAuth) (C-auth) ([b0ec893](https://github.com/bhargavamin/contexer/commit/b0ec893d9e25877725c891f70b228547f462d437))
* add delta-poll injection (C7) ([9080fc9](https://github.com/bhargavamin/contexer/commit/9080fc9a2b19738f29e25a9ed13440c061087e2b))
* add delta-poll injection (C7) ([113bee1](https://github.com/bhargavamin/contexer/commit/113bee1cc63b814cc9531b72cb8b07e13960234d))
* add local-fallback degradation for RemoteStore (C8) ([54530d9](https://github.com/bhargavamin/contexer/commit/54530d9132eaf2462580a20b701b255b9dd9c830))
* add local-fallback degradation for RemoteStore (C8) ([da6949a](https://github.com/bhargavamin/contexer/commit/da6949a7dcae2173c1435900645ca02080477b2d))
* add RemoteStore MCP sync client (C3) ([447afde](https://github.com/bhargavamin/contexer/commit/447afde76b28028b7509e9d1d5074e0692b4bfb7))
* add RemoteStore MCP sync client (C3) ([027a4c9](https://github.com/bhargavamin/contexer/commit/027a4c92b7a4cbdc6a87ea93da0d1baacbfdbbcc))
* add share_decision + contexer share (C4) ([0f9fbe5](https://github.com/bhargavamin/contexer/commit/0f9fbe53aaea2faed6df017d7d9814412bbdfdbe))
* add share_decision + contexer share (C4) ([bad6554](https://github.com/bhargavamin/contexer/commit/bad6554e584bf6440627dc9b1ff87bd2d185045b))
* add team-context pull + merge (C5) ([fb73cbe](https://github.com/bhargavamin/contexer/commit/fb73cbea76458132fb575d22a2e5771394b23dfd))
* add team-context pull + merge (C5) ([1460c2e](https://github.com/bhargavamin/contexer/commit/1460c2ee32979e5eb31162bcb97e8a42f25b4ac5))
* clean team onboarding — opt-in native MCP + self-configuring login ([e4afd6e](https://github.com/bhargavamin/contexer/commit/e4afd6e4c0d677063dbf88a27966b3ec138228af))
* clean team onboarding (opt-in native MCP + self-configuring login) ([e16375a](https://github.com/bhargavamin/contexer/commit/e16375a5061b3bbaecb8e0226cf8400b83ed25c2))
* make C7 delta-poll non-blocking (background refresh off the prompt path) ([a0107fb](https://github.com/bhargavamin/contexer/commit/a0107fb426b09625a11778222b4731dc85d05a45))
* non-blocking C7 delta-poll + inclusive-cursor re-injection fix ([cf71923](https://github.com/bhargavamin/contexer/commit/cf71923875b971e05c550b69e9c58271c0889dc7))
* opt-in native teams MCP entry respects the configured endpoint ([9c414cf](https://github.com/bhargavamin/contexer/commit/9c414cf3248b02d115ab9a6c268597dd97b490b3))
* zero-config remote OAuth on install (contexer-teams) ([00a7371](https://github.com/bhargavamin/contexer/commit/00a7371a09ee536fd874e1445461e4bc31ddb505))


### Bug Fixes

* don't re-inject unchanged rows re-sent by an inclusive updatedSince cursor ([3404445](https://github.com/bhargavamin/contexer/commit/3404445f7e961a2fed127859c423a52663e0b1ec))
* remove PostCompact hook (visible noise, injected nothing) ([#76](https://github.com/bhargavamin/contexer/issues/76)) ([8906a1e](https://github.com/bhargavamin/contexer/commit/8906a1e82a0dfc0e36178a446a330fc4571fae58))
* single-source Teams endpoint on the stable domain + harden login ([b4290d6](https://github.com/bhargavamin/contexer/commit/b4290d6d738b1035942adf6f7d1c51752076c796))
* single-source Teams endpoint on the stable domain + harden login ([576b510](https://github.com/bhargavamin/contexer/commit/576b51009a9ed3d7e7213f730b78ea17952036ee))


### Documentation

* **readme:** lead with cross-tool + aha demo; add Contexer Teams (governance) ([#67](https://github.com/bhargavamin/contexer/issues/67)) ([be4a666](https://github.com/bhargavamin/contexer/commit/be4a66609a6c65e9c7ee3cdf1747ee7db4342963))

## [0.13.0](https://github.com/bhargavamin/contexer/compare/v0.12.0...v0.13.0) (2026-07-03)


### Features

* **cli:** guard uninstall --purge behind a confirmation ([00a1460](https://github.com/bhargavamin/contexer/commit/00a1460e295fbe4853225532959f46b65b07ebdf))
* **cli:** guard uninstall --purge behind a yes/no confirmation ([cf99c48](https://github.com/bhargavamin/contexer/commit/cf99c4899abbef3f08d1a3d73cf0e621ac635aa1))
* maturity-aware decision lifecycle + plan-mode capture ([#65](https://github.com/bhargavamin/contexer/issues/65)) ([5c31e93](https://github.com/bhargavamin/contexer/commit/5c31e937804c29e50321bf54e2846900ef740615))

## [0.12.0](https://github.com/bhargavamin/contexer/compare/v0.11.0...v0.12.0) (2026-06-30)


### Features

* **sync:** config.toml profile loader + canonical repo-key ([#60](https://github.com/bhargavamin/contexer/issues/60)) ([26851eb](https://github.com/bhargavamin/contexer/commit/26851eb363c02c173cd82d6d890b487b24166eaa))

## [0.11.0](https://github.com/bhargavamin/contexer/compare/v0.10.0...v0.11.0) (2026-06-29)


### Features

* trusted, versioned decision model with human review ([#56](https://github.com/bhargavamin/contexer/issues/56)) ([431b095](https://github.com/bhargavamin/contexer/commit/431b09515498470c0cdca80a7c7805f98c2fc696))


### Documentation

* reposition README as the engineering decision layer ([#55](https://github.com/bhargavamin/contexer/issues/55)) ([8095cbd](https://github.com/bhargavamin/contexer/commit/8095cbd615bf0beaf64bbeba84ba06a63e5a1d50))

## [0.10.0](https://github.com/bhargavamin/contexer/compare/v0.9.0...v0.10.0) (2026-06-25)


### Features

* **adapters:** add Gemini CLI integration ([#50](https://github.com/bhargavamin/contexer/issues/50)) ([6b39991](https://github.com/bhargavamin/contexer/commit/6b3999164a92786e5885c87fc5d1fe5242afeea2))


### Documentation

* logo updates, formatting fixes, and factual corrections ([#53](https://github.com/bhargavamin/contexer/issues/53)) ([0307a07](https://github.com/bhargavamin/contexer/commit/0307a07eef2ddda69f467c58d5c6c7c9d65573f9))
* marketing-first README rewrite ([#52](https://github.com/bhargavamin/contexer/issues/52)) ([5eb78c2](https://github.com/bhargavamin/contexer/commit/5eb78c2b3f7ba5bbc1e57e34e35ede69803c01fb))

## [0.9.0](https://github.com/bhargavamin/contexer/compare/v0.8.0...v0.9.0) (2026-06-23)


### Features

* **adapters:** add Codex integration with near-full Claude parity ([#48](https://github.com/bhargavamin/contexer/issues/48)) ([4352f22](https://github.com/bhargavamin/contexer/commit/4352f228d5320b16be7fe2aaed5a121ea21a758c))


### Bug Fixes

* resolve critical + high-severity review findings (store, hooks, plugin) ([#47](https://github.com/bhargavamin/contexer/issues/47)) ([59614b2](https://github.com/bhargavamin/contexer/commit/59614b2117bafa151a751240bcba153795a49f5f))

## [0.8.0](https://github.com/bhargavamin/contexer/compare/v0.7.0...v0.8.0) (2026-06-19)


### Features

* **memory-sync:** import Claude Code memory-tool facts into the store ([#46](https://github.com/bhargavamin/contexer/issues/46)) ([1aa9f77](https://github.com/bhargavamin/contexer/commit/1aa9f77ada6faf8d1d0e975fdefb46f7e411ba6a))


### Documentation

* tool-neutral README rewrite + security policy ([#44](https://github.com/bhargavamin/contexer/issues/44)) ([6b7b53e](https://github.com/bhargavamin/contexer/commit/6b7b53ebcae9548438eb54fde75b4f2b72d6cef8))

## [0.7.0](https://github.com/bhargavamin/contexer/compare/v0.6.3...v0.7.0) (2026-06-15)


### Features

* multi-provider adapter + Cursor integration ([#41](https://github.com/bhargavamin/contexer/issues/41)) ([9578bf7](https://github.com/bhargavamin/contexer/commit/9578bf7c4083e239836ff826024b928cb497271c))
* **store:** recurrence counter + explicit pattern subtype ([#40](https://github.com/bhargavamin/contexer/issues/40)) ([5f80125](https://github.com/bhargavamin/contexer/commit/5f80125cfe605e80011db87d056d58a5bab53f69))

## [0.6.3](https://github.com/bhargavamin/contexer/compare/v0.6.2...v0.6.3) (2026-06-12)


### Bug Fixes

* **store:** fix _TRAILING_FILLER stripping henceforth directives and classify as-a-rule as convention ([#35](https://github.com/bhargavamin/contexer/issues/35)) ([7046597](https://github.com/bhargavamin/contexer/commit/7046597102261e18b78eeabad16e749c7acf0005))


### Documentation

* **readme:** top-down rewrite — lead with problem and before/after ([#39](https://github.com/bhargavamin/contexer/issues/39)) ([6d84af9](https://github.com/bhargavamin/contexer/commit/6d84af9995d7fea5c8b8b16c1136aa987db467c6))
