# Changelog

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
