# TxLINE off-chain API for the Hybrid on-chain/off-chain TxODDS Data system — tools

18 first-call-correct tools. Auth is injected at call time and never appears here.

## postAuthGuestStart
Start a new guest session

`POST /auth/guest/start`

## postApiTokenActivate
Activate subscription and retrieve API token

`POST /api/token/activate`

Inputs: `body`* (object)

## postApiGuestPurchaseQuote
Request a partially signed purchase quote given the wallet public key and required TxLINE amount in whole units

`POST /api/guest/purchase/quote`

Inputs: `body`* (object)

## getApiFixturesSnapshot
Get the latest snapshot of fixtures, optionally starting at or within 30 days after a given epoch day Optional: startEpochDay, competitionId.

`GET /api/fixtures/snapshot`

Inputs: `startEpochDay` (integer), `competitionId` (integer)

## getApiFixturesUpdatesEpochdayHourofday
Get all fixture updates for a single fixture on a given day Required: epochDay, hourOfDay.

`GET /api/fixtures/updates/{epochDay}/{hourOfDay}`

Inputs: `epochDay`* (integer), `hourOfDay`* (integer)

## getApiFixturesValidation
Get a Merkle proof for a specific fixture update Required: fixtureId. Optional: timestamp.

`GET /api/fixtures/validation`

Inputs: `fixtureId`* (integer), `timestamp` (integer)

## getApiFixturesBatch-validation
Get a Merkle proof for an entire hourly batch of fixtures Required: epochDay, hourOfDay.

`GET /api/fixtures/batch-validation`

Inputs: `epochDay`* (integer), `hourOfDay`* (integer)

## getApiOddsSnapshotFixtureid
Get snapshots of the latest odds for a fixture Required: fixtureId. Optional: asOf.

`GET /api/odds/snapshot/{fixtureId}`

Inputs: `fixtureId`* (integer), `asOf` (integer)

## getApiOddsUpdatesFixtureid
Get currently live odds updates for a single fixture Required: fixtureId.

`GET /api/odds/updates/{fixtureId}`

Inputs: `fixtureId`* (integer)

## getApiOddsUpdatesEpochdayHourofdayInterval
Get a json array of all odd updates from a specific historical 5-minute interval Required: epochDay, hourOfDay, interval. Optional: fixtureId.

`GET /api/odds/updates/{epochDay}/{hourOfDay}/{interval}`

Inputs: `epochDay`* (integer), `hourOfDay`* (integer), `interval`* (integer), `fixtureId` (integer)

## getApiOddsStream
Get a real-time Server-Sent Events stream of odds updates Optional: fixtureId, Last-Event-ID.

`GET /api/odds/stream`

Inputs: `fixtureId` (integer), `Last-Event-ID` (string)

## getApiOddsValidation
Get a Merkle proof for a specific odds update Required: messageId, ts.

`GET /api/odds/validation`

Inputs: `messageId`* (string), `ts`* (integer)

## getApiScoresSnapshotFixtureid
Get snapshots for each action in the latest score events for a fixture Required: fixtureId. Optional: asOf.

`GET /api/scores/snapshot/{fixtureId}`

Inputs: `fixtureId`* (integer), `asOf` (integer)

## getApiScoresUpdatesEpochdayHourofdayInterval
Get a json array of all score updates from a specific historical 5-minute interval (no live data is returned) Required: epochDay, hourOfDay, interval. Optional: fixtureId.

`GET /api/scores/updates/{epochDay}/{hourOfDay}/{interval}`

Inputs: `epochDay`* (integer), `hourOfDay`* (integer), `interval`* (integer), `fixtureId` (integer)

## getApiScoresUpdatesFixtureid
Get the sequence of score updates for a single fixture within the current 5-min interval Required: fixtureId.

`GET /api/scores/updates/{fixtureId}`

Inputs: `fixtureId`* (integer)

## getApiScoresHistoricalFixtureid
Get the full sequence of score updates for a single fixture Required: fixtureId.

`GET /api/scores/historical/{fixtureId}`

Inputs: `fixtureId`* (integer)

## getApiScoresStream
Get a real-time Server-Sent Events stream of scores updates Optional: fixtureId, Last-Event-ID.

`GET /api/scores/stream`

Inputs: `fixtureId` (integer), `Last-Event-ID` (string)

## getApiScoresStat-validation
Get a three-stage Merkle proof for a single score statistic Required: fixtureId, seq, statKey. Optional: statKey2.

`GET /api/scores/stat-validation`

Inputs: `fixtureId`* (integer), `seq`* (integer), `statKey`* (integer), `statKey2` (integer)
