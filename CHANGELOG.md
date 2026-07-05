# Changelog

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
