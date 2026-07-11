# Changelog

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
