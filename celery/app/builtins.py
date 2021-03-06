# -*- coding: utf-8 -*-
"""
    celery.app.builtins
    ~~~~~~~~~~~~~~~~~~~

    Built-in tasks that are always available in all
    app instances. E.g. chord, group and xmap.

"""
from __future__ import absolute_import

from celery._state import get_current_worker_task, connect_on_app_finalize
from celery.utils.log import get_logger

__all__ = []

logger = get_logger(__name__)


@connect_on_app_finalize
def add_backend_cleanup_task(app):
    """The backend cleanup task can be used to clean up the default result
    backend.

    If the configured backend requires periodic cleanup this task is also
    automatically configured to run every day at midnight (requires
    :program:`celery beat` to be running).

    """
    @app.task(name='celery.backend_cleanup',
              shared=False, _force_evaluate=True)
    def backend_cleanup():
        app.backend.cleanup()
    return backend_cleanup


@connect_on_app_finalize
def add_unlock_chord_task(app):
    """This task is used by result backends without native chord support.

    It joins chords by creating a task chain polling the header for completion.

    """
    from celery.canvas import maybe_signature
    from celery.exceptions import ChordError
    from celery.result import allow_join_result, result_from_tuple

    default_propagate = app.conf.CELERY_CHORD_PROPAGATES

    @app.task(name='celery.chord_unlock', max_retries=None, shared=False,
              default_retry_delay=1, ignore_result=True, _force_evaluate=True)
    def unlock_chord(group_id, callback, interval=None, propagate=None,
                     max_retries=None, result=None,
                     Result=app.AsyncResult, GroupResult=app.GroupResult,
                     result_from_tuple=result_from_tuple):
        # if propagate is disabled exceptions raised by chord tasks
        # will be sent as part of the result list to the chord callback.
        # Since 3.1 propagate will be enabled by default, and instead
        # the chord callback changes state to FAILURE with the
        # exception set to ChordError.
        propagate = default_propagate if propagate is None else propagate
        if interval is None:
            interval = unlock_chord.default_retry_delay

        # check if the task group is ready, and if so apply the callback.
        callback = maybe_signature(callback, app)
        deps = GroupResult(
            group_id,
            [result_from_tuple(r, app=app) for r in result],
        )
        j = deps.join_native if deps.supports_native_join else deps.join

        if deps.ready():
            callback = maybe_signature(callback, app=app)
            try:
                with allow_join_result():
                    ret = j(timeout=3.0, propagate=propagate)
            except Exception as exc:
                try:
                    culprit = next(deps._failed_join_report())
                    reason = 'Dependency {0.id} raised {1!r}'.format(
                        culprit, exc,
                    )
                except StopIteration:
                    reason = repr(exc)
                logger.error('Chord %r raised: %r', group_id, exc, exc_info=1)
                app.backend.chord_error_from_stack(callback,
                                                   ChordError(reason))
            else:
                try:
                    callback.delay(ret)
                except Exception as exc:
                    logger.error('Chord %r raised: %r', group_id, exc,
                                 exc_info=1)
                    app.backend.chord_error_from_stack(
                        callback,
                        exc=ChordError('Callback error: {0!r}'.format(exc)),
                    )
        else:
            raise unlock_chord.retry(countdown=interval,
                                     max_retries=max_retries)
    return unlock_chord


@connect_on_app_finalize
def add_map_task(app):
    from celery.canvas import signature

    @app.task(name='celery.map', shared=False, _force_evaluate=True)
    def xmap(task, it):
        task = signature(task, app=app).type
        return [task(item) for item in it]
    return xmap


@connect_on_app_finalize
def add_starmap_task(app):
    from celery.canvas import signature

    @app.task(name='celery.starmap', shared=False, _force_evaluate=True)
    def xstarmap(task, it):
        task = signature(task, app=app).type
        return [task(*item) for item in it]
    return xstarmap


@connect_on_app_finalize
def add_chunk_task(app):
    from celery.canvas import chunks as _chunks

    @app.task(name='celery.chunks', shared=False, _force_evaluate=True)
    def chunks(task, it, n):
        return _chunks.apply_chunks(task, it, n)
    return chunks


@connect_on_app_finalize
def add_group_task(app):
    """No longer used, but here for backwards compatibility."""
    _app = app
    from celery.canvas import maybe_signature
    from celery.result import result_from_tuple

    class Group(app.Task):
        app = _app
        name = 'celery.group'
        _decorated = True

        def run(self, tasks, result, group_id, partial_args,
                add_to_parent=True):
            app = self.app
            result = result_from_tuple(result, app)
            # any partial args are added to all tasks in the group
            taskit = (maybe_signature(task, app=app).clone(partial_args)
                      for i, task in enumerate(tasks))
            with app.producer_or_acquire() as pub:
                [stask.apply_async(group_id=group_id, producer=pub,
                                   add_to_parent=False) for stask in taskit]
            parent = get_current_worker_task()
            if add_to_parent and parent:
                parent.add_trail(result)
            return result
    return Group


@connect_on_app_finalize
def add_chain_task(app):
    """No longer used, but here for backwards compatibility."""
    _app = app

    class Chain(app.Task):
        app = _app
        name = 'celery.chain'
        _decorated = True

    return Chain


@connect_on_app_finalize
def add_chord_task(app):
    """No longer used, but here for backwards compatibility."""
    from celery import group, chord as _chord
    from celery.canvas import maybe_signature
    _app = app

    class Chord(app.Task):
        app = _app
        name = 'celery.chord'
        ignore_result = False
        _decorated = True

        def run(self, header, body, partial_args=(), interval=None,
                countdown=1, max_retries=None, propagate=None,
                eager=False, **kwargs):
            app = self.app
            # - convert back to group if serialized
            tasks = header.tasks if isinstance(header, group) else header
            header = group([
                maybe_signature(s, app=app) for s in tasks
            ], app=self.app)
            body = maybe_signature(body, app=app)
            ch = _chord(header, body)
            return ch.run(header, body, partial_args, app, interval,
                          countdown, max_retries, propagate, **kwargs)
    return Chord
