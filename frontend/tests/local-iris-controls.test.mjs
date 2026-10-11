import assert from 'node:assert/strict';
import test from 'node:test';
import { localIrisControls } from '../src/local-iris-controls.ts';

function readyStatus(extra = {}) {
  return {
    enabled: true,
    connected: true,
    operator_ready: true,
    context: 'docker-desktop',
    namespace: 'strawhats-operator-local',
    name: 'iris',
    phase: 'Ready',
    mode: 'Deploy',
    image: 'strawhats-iris:milestone-one',
    reported_image: 'strawhats-iris:milestone-one',
    desired_replicas: 1,
    ready_replicas: 1,
    generation: 3,
    observed_generation: 3,
    rollback_available: true,
    message: 'Verified',
    ...extra,
  };
}

test('verified status enables bounded lifecycle actions', () => {
  assert.deepEqual(localIrisControls(readyStatus(), false, false), {
    deploy: true,
    scale: true,
    rollback: true,
  });
});

test('missing, stale, or busy status never enables writes', () => {
  const disabled = { deploy: false, scale: false, rollback: false };
  assert.deepEqual(localIrisControls(null, false, false), disabled);
  assert.deepEqual(localIrisControls(readyStatus(), true, false), disabled);
  assert.deepEqual(localIrisControls(readyStatus(), false, true), disabled);
});

test('unavailable API, controller, or disabled integration never enables writes', () => {
  for (const status of [
    readyStatus({ enabled: false }),
    readyStatus({ connected: false }),
    readyStatus({ operator_ready: false }),
    readyStatus({ phase: 'Unavailable' }),
  ]) {
    assert.deepEqual(localIrisControls(status, false, false), {
      deploy: false,
      scale: false,
      rollback: false,
    });
  }
});

test('pending or unobserved generations cannot scale or claim readiness', () => {
  assert.equal(
    localIrisControls(readyStatus({ phase: 'Pending' }), false, false).scale,
    false,
  );
  assert.equal(
    localIrisControls(readyStatus({ generation: 4 }), false, false).scale,
    false,
  );
});

test('failed rollout can redeploy and restore its verified snapshot', () => {
  assert.deepEqual(
    localIrisControls(readyStatus({ phase: 'Failed' }), false, false),
    {
      deploy: true,
      scale: false,
      rollback: true,
    },
  );
});

test('a restored configuration can be scaled, while missing snapshots cannot roll back', () => {
  assert.equal(
    localIrisControls(
      readyStatus({ phase: 'RolledBack', mode: 'Rollback' }),
      false,
      false,
    ).scale,
    true,
  );
  assert.equal(
    localIrisControls(readyStatus({ rollback_available: false }), false, false)
      .rollback,
    false,
  );
});

test('an absent model can deploy but cannot scale or roll back', () => {
  assert.deepEqual(
    localIrisControls(
      readyStatus({
        phase: 'NotDeployed',
        generation: null,
        observed_generation: null,
        rollback_available: false,
        desired_replicas: null,
        ready_replicas: 0,
      }),
      false,
      false,
    ),
    { deploy: true, scale: false, rollback: false },
  );
});
