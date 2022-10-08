from __future__ import annotations

import abc
import asyncio
import gc
import logging
import random
import time
import traceback
import uuid
import weakref
from enum import Enum
from typing import Any, Dict, Generic, List, Optional, Set, Tuple, Type, TypeVar, Union
from uuid import UUID

import faust
from faust import App, Record, Topic
from mode import Service

import dagger

logger = logging.getLogger(__name__)


KT = TypeVar("KT", str, int, bytes)
VT = TypeVar("VT", str, Record, bytes)
T = TypeVar("T")

TaskLookupKey = Tuple[KT, VT]
COMPLETE_BY_KEY = "complete_by_time"  # time in seconds


class TaskStatusEnum(Enum):
    NOT_STARTED = "NOT_STARTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"
    FAILURE = "FAILURE"
    SKIPPED = "SKIPPED"
    SUBMITTED = "SUBMITTED"
    STOPPED = "STOPPED"


TERMINAL_STATUSES = [
    TaskStatusEnum.COMPLETED.name,
    TaskStatusEnum.SKIPPED.name,
    TaskStatusEnum.FAILURE.name,
    TaskStatusEnum.STOPPED.name,
]


class TaskType(Enum):
    ROOT = "ROOT"
    LEAF = "LEAF"
    SUB_DAG = "SUB_DAG"
    PARALLEL_COMPOSITE = "PARALLEL_COMPOSITE"


class TaskStatus(Record, serializer="raw"):  # type: ignore
    code: str = TaskStatusEnum.NOT_STARTED.name
    value: str = TaskStatusEnum.NOT_STARTED.value


class ITask(Record, Generic[KT, VT], serializer="raw"):  # type: ignore
    """Class that every template, process, and task extends. Defines attributes and core functions that Dagger uses."""

    id: UUID
    time_submitted: int = 0
    time_completed: int = 0
    lastupdated: int = int(time.time())
    task_name: str = None
    task_type: str = TaskType.LEAF.name
    parent_id: UUID = None
    status: TaskStatus = TaskStatus()
    time_created: int = int(time.time())
    next_dags: List[UUID] = list()
    root_dag: UUID = None
    message: str = None
    allow_skip_to: bool = False
    reprocess_on_message: bool = False
    correlatable_key: KT = None

    def get_id(self) -> UUID:
        return self.id

    @abc.abstractmethod
    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:  # pragma: no cover
        """Executes the ITask."""
        ...

    @abc.abstractmethod
    async def stop(self) -> None:  # pragma: no cover
        """Stops the ITask."""
        ...

    @abc.abstractmethod
    async def on_message(
        self, runtime_parameters: Dict[str, str], *args: Any, **kwargs: Any
    ) -> bool:  # pragma: no cover
        """Defines what to do when the task recieves a message."""
        ...

    @abc.abstractmethod
    async def evaluate(self, **kwargs: Any) -> Optional[UUID]:  # pragma: no cover
        """Processes some inputs and determines the next ITask id.

        Returns:
            Optional[UUID]: The next ITask id.
        """
        ...

    async def notify(
        self, status: TaskStatus, workflow_instance: ITask
    ) -> None:  # pragma: no cover
        """If not completed, runs the steps required for completion by calling on_complete()."""
        if self.status.code != status.code:
            await self.on_complete(status=status, workflow_instance=workflow_instance)

    @abc.abstractmethod
    def get_correlatable_key(self, payload: Any) -> TaskLookupKey:  # pragma: no cover
        """Get the lookup key,value associated with the task.Deprecated use get_correlatable_key_from_payload

        Args:
            payload (Any): The lookup key,value.

        Returns:
            TaskLookupKey: Can be used to associate a task with a message.
        """
        ...

    async def get_correlatable_key_from_payload(
        self, payload: Any
    ) -> TaskLookupKey:  # pragma: no cover
        """Get the lookup key,value associated with the task(Deprecated use get_correlatable_keys_from_payload).

        Args:
            payload (Any): The lookup key,value.

        Returns:
            TaskLookupKey: Can be used to associate a task with a message.
        """
        return self.get_correlatable_key(payload=payload)

    async def get_correlatable_keys_from_payload(
        self, payload: Any
    ) -> List[TaskLookupKey]:  # pragma: no cover
        """Get a list of lookup key,value associated with the task(s).

        Args:
            payload (Any): The lookup key,value.

        Returns:
            List[TaskLookupKey]: Can be used to associate task(s) with a message.
        """
        return [await self.get_correlatable_key_from_payload(payload=payload)]

    @abc.abstractmethod
    async def start(self, workflow_instance: ITask) -> None:  # pragma: no cover
        """Starts the ITask."""
        ...

    async def get_remaining_tasks(
        self,
        next_dag_id: UUID,
        workflow_instance: ITask,
        tasks: List[ITask] = None,
        end_task_id: UUID = None,
    ) -> List[ITask]:
        """Get the remaining tasks in the template.

        Args:
            next_dag_id (UUID): Current ITask id.
            tasks (List[ITask], optional): List of previous ITasks. Defaults to [].
            end_task (ITask): The task that the function should stop and return at. Defaults to None (so end of DAG).

        Returns:
            List[ITask]: List of remaining ITasks appended to inputted list.
        """
        if next_dag_id == workflow_instance.id:
            task_instance = workflow_instance
        else:
            task_instance = workflow_instance.get_task(id=next_dag_id)
        if not task_instance:
            logger.warning(
                f"Could not delete instance of itask with id: '{next_dag_id}' as it does not exist. Skipping."
            )
            return tasks
        if task_instance.root_dag:
            await self.get_remaining_tasks(
                task_instance.root_dag, workflow_instance, tasks, end_task_id
            )
        if task_instance.get_id() == end_task_id:
            tasks.append(task_instance)
            return tasks
        if tasks and tasks[-1].get_id() == end_task_id:
            return tasks
        else:
            tasks.append(task_instance)
            for next_dag_id in task_instance.next_dags:
                await self.get_remaining_tasks(
                    next_dag_id, workflow_instance, tasks, end_task_id
                )
        return tasks

    async def on_complete(
        self,
        workflow_instance: ITask,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        *,
        iterate: bool = True,
    ) -> None:
        """Sets the status of the ITask to completed and starts the next ITask if there is one."""
        # get the parent
        if self.status.code != status.code:
            self.status = status
            if self.time_completed > 0:
                time_completed = (
                    self.time_completed
                )  # if time_completed is set by the application honor that
            else:
                time_completed = int(time.time())
            self.time_completed = time_completed
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        if not iterate:
            logger.debug("Skipping on_complete as iterate is false")
            return
        next_task_submitted = False
        for next_dag_id in self.next_dags:
            await asyncio.sleep(0)
            next_dag_instance = workflow_instance.get_task(id=next_dag_id)  # type: ignore
            if not next_dag_instance:
                logger.error(
                    f"Could not find the next DAG in the template with id: {next_dag_id} and thus unable to set to complete. Skipping."
                )
                continue
            next_task_submitted = True
            if next_dag_instance.status.code == TaskStatusEnum.SKIPPED.name:
                logger.info(f"Skipping skipped task {next_dag_instance} {next_dag_id}")
            else:
                return await next_dag_instance.start(
                    workflow_instance=workflow_instance
                )
        if next_task_submitted is False and self.parent_id:
            parent_node = workflow_instance.get_task(id=self.parent_id)
            if parent_node:
                parent_node.time_completed = self.time_completed
                await parent_node.notify(
                    status=status, workflow_instance=workflow_instance
                )
            else:
                logger.error(
                    f"Unable to retrieve parent node for task with id: {str(self.get_id())}"
                )
        elif self.task_type == TaskType.ROOT.name:
            subdags_in_non_terminating_state = False
            logger.debug(f"Executing root dag cleanup {str(workflow_instance.id)}")
            for task in workflow_instance.tasks.values():
                await asyncio.sleep(0)
                if task and task.status.code not in [
                    TaskStatusEnum.COMPLETED.name,
                    TaskStatusEnum.SKIPPED.name,
                    TaskStatusEnum.FAILURE.name,
                    TaskStatusEnum.STOPPED.name,
                ]:
                    subdags_in_non_terminating_state = True

                await dagger.service.services.Dagger.app.remove_task_from_correletable_keys_table(task, workflow_instance=workflow_instance)  # type: ignore
                m_task = await dagger.service.services.Dagger.app.get_monitoring_task(task=task, workflow_instance=workflow_instance)  # type: ignore
                if m_task:
                    await m_task.on_complete(workflow_instance=workflow_instance)
                    await dagger.service.services.Dagger.app._store.process_trigger_task_complete(  # type: ignore
                        m_task, wokflow_instance=workflow_instance
                    )  # type: ignore
            if dagger.service.services.Dagger.app.delete_workflow_on_complete:  # type: ignore
                await dagger.service.services.Dagger.app._remove_root_template_instance(self)  # type: ignore
                logger.info(f"Removed references to root task: {self.id}")
            if subdags_in_non_terminating_state:
                logger.info(
                    f"One or more sub dags are still in non terminated state",
                    extra={"root_dag_id": workflow_instance.id},
                )


class ExecutorTask(ITask[KT, VT], abc.ABC):
    async def start(
        self, workflow_instance: ITask, ignore_status: bool = False
    ) -> None:
        # pre-execute
        if self.status.code in [
            TaskStatusEnum.COMPLETED.name,
            TaskStatusEnum.SKIPPED.name,
        ]:
            return await self.on_complete(
                status=self.status, workflow_instance=workflow_instance
            )
        if ignore_status or self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            await self.execute(
                runtime_parameters=workflow_instance.runtime_parameters,
                workflow_instance=workflow_instance,
            )
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        if self.status.code == TaskStatusEnum.FAILURE.name:
            await self.on_complete(
                status=self.status, workflow_instance=workflow_instance
            )
        else:
            await self.on_complete(workflow_instance=workflow_instance)

    async def evaluate(self, **kwargs: Any) -> Optional[UUID]:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Executor does not evaluate")

    async def on_message(
        self, runtime_parameters: Dict[str, str], *args: Any, **kwargs: Any
    ) -> bool:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Executor does not process on_message")


class TriggerTask(ExecutorTask[KT, VT], abc.ABC):
    time_to_execute: int = None

    async def start(
        self, workflow_instance: ITemplateDAGInstance, ignore_status=True
    ) -> None:
        await asyncio.sleep(0)
        if self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
        if not self.time_to_execute or int(time.time()) >= self.time_to_execute:
            logger.info(
                f"Trigger task {self.id} triggered for trigger time {self.time_to_execute} "
            )
            await super().start(workflow_instance=workflow_instance, ignore_status=True)
        else:
            logger.warning(
                f"Trigger task {self.id} {time.time()} cannot be triggered for trigger time {self.time_to_execute}"
            )

    async def on_complete(
        self,
        workflow_instance: ITask,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        iterate=True,
    ) -> None:
        await dagger.service.services.Dagger.app._store.process_trigger_task_complete(self, wokflow_instance=workflow_instance)  # type: ignore
        await super().on_complete(workflow_instance=workflow_instance, iterate=iterate)


class IntervalTask(TriggerTask[KT, VT], abc.ABC):
    time_to_force_complete: int = None  # time in seconds
    interval_execute_period: int = None  # time in seconds

    async def start(self, workflow_instance: ITask) -> bool:  # type: ignore
        is_finished = False
        if self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        if self.time_to_execute and int(time.time()) < self.time_to_execute:
            return False
        if not self.time_to_execute or int(time.time()) >= self.time_to_execute:
            logger.info(
                f"Interval task {self.id} executed on interval of {self.interval_execute_period} "
            )
            is_finished = await self.interval_execute(
                workflow_instance.runtime_parameters
            )
            if not is_finished and self.interval_execute_period:
                self.time_to_execute = int(time.time()) + self.interval_execute_period
                await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
                await dagger.service.services.Dagger.app._store_trigger_instance(self, workflow_instance=workflow_instance)  # type: ignore
        if is_finished or (
            self.time_to_force_complete
            and int(time.time()) >= self.time_to_force_complete
        ):
            await super().start(
                ignore_status=False, workflow_instance=workflow_instance
            )
            return True
        return False

    async def interval_execute(self, runtime_parameters: Dict[str, VT]) -> bool:
        """Task to run on an interval until either the trigger end time or until this method returns True.

        Returns:
            bool: If True, finish this task.
        """
        return True


class MonitoringTask(TriggerTask[KT, VT], abc.ABC):
    monitored_task_id: UUID = None

    @abc.abstractmethod
    async def process_monitored_task(
        self, monitored_task: ITask, workflow_instance: ITask
    ) -> None:  # pragma: no cover
        """
        Callback on when business logic has to be executed on the monitored task based on the time condition
        :param monitored_task: the monitored task
        :return: None
        """
        ...

    async def on_complete(
        self,
        workflow_instance: ITask,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        iterate=True,
    ) -> None:
        await super().on_complete(
            workflow_instance=workflow_instance, status=status, iterate=iterate
        )


class DefaultMonitoringTask(MonitoringTask[str, str]):
    async def execute(
        self,
        runtime_parameters: Dict[str, str],
        workflow_instance: ITemplateDAGInstance = None,
    ) -> None:
        logger.info(
            f"Executing DefaultMonitoringTask {self.id} monitoring for {self.monitored_task_id}"
        )
        # check the status of the monitored task
        monitored_task = workflow_instance.get_task(id=self.monitored_task_id)  # type: ignore
        if monitored_task and monitored_task.status.code not in [
            TaskStatusEnum.COMPLETED.name,
            TaskStatusEnum.SKIPPED.name,
            TaskStatusEnum.FAILURE.name,
        ]:
            logger.info(f"Processing the monitored task {self.monitored_task_id}")
            await self.process_monitored_task(
                monitored_task=monitored_task, workflow_instance=workflow_instance
            )


class SkipOnMaxDurationTask(DefaultMonitoringTask):
    async def process_monitored_task(
        self, monitored_task: ITask, workflow_instance: ITask
    ) -> None:  # pragma: no cover
        if monitored_task.status.code == TaskStatusEnum.EXECUTING.name:
            if monitored_task:
                logger.info(
                    f"Process: {monitored_task.process_name} with id: {monitored_task.id} did not finish before it's timeout. Skipping."
                )

                all_prev_dags = await self.get_remaining_tasks(
                    workflow_instance.get_id(),
                    workflow_instance,
                    [],
                    end_task_id=monitored_task.get_id(),
                )
                skipped_task_status = TaskStatus(
                    code=TaskStatusEnum.SKIPPED.name, value=TaskStatusEnum.SKIPPED.value
                )
                for dag in all_prev_dags[:-1]:
                    if dag.status.code in [
                        TaskStatusEnum.EXECUTING.value,
                        TaskStatusEnum.NOT_STARTED.value,
                    ]:
                        await dag.on_complete(
                            status=skipped_task_status,
                            workflow_instance=workflow_instance,
                            iterate=False,
                        )
                await monitored_task.on_complete(
                    workflow_instance=workflow_instance, status=skipped_task_status
                )


class DecisionTask(ITask[KT, VT]):
    async def start(self, workflow_instance: ITask) -> None:
        # pre-execute
        if self.status.code in [
            TaskStatusEnum.COMPLETED.name,
            TaskStatusEnum.SKIPPED.name,
        ]:
            return await self.on_complete(workflow_instance=workflow_instance)
        if self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            task_to_execute = await self.evaluate(
                **workflow_instance.runtime_parameters
            )
            for next_task_id in self.next_dags:
                if next_task_id != task_to_execute:
                    task_to_skip = workflow_instance.get_taskt(id=next_task_id)  # type: ignore
                    if task_to_skip:
                        task_to_skip.status = TaskStatus(
                            code=TaskStatusEnum.SKIPPED.name,
                            value=TaskStatusEnum.SKIPPED.value,
                        )
                    else:
                        logger.warning(
                            f"The task instance to skip with id {next_task_id} was not found. Skipped but did not set status to {TaskStatusEnum.SKIPPED.value}"
                        )
        await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        await self.on_complete(workflow_instance=workflow_instance)

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Decision task does not process execute")

    async def on_message(
        self, runtime_parameters: Dict[str, str], *args: Any, **kwargs: Any
    ) -> bool:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Decision task does not process get_correlatable_key")

    async def evaluate(self, **kwargs: Any) -> Optional[UUID]:
        num = random.randint(0, 1)  # nosec
        if num == 0:
            return None
        else:
            for task_id in self.next_dags:
                return task_id
        return None


class SystemTask(ExecutorTask[str, str]):
    async def on_message(self, *args: Any, **kwargs: Any) -> bool:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("SystemTask task does not process on_message")

    async def evaluate(self, **kwargs: Any) -> Optional[UUID]:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("SystemTask task does not process on_message")

    def get_correlatable_key(self, payload: Any) -> TaskLookupKey:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError(
            "SystemTask task does not process get_correlatable_key"
        )

    async def on_complete(
        self,
        workflow_instance: ITask,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        iterate=True,
    ) -> None:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("SystemTask task does not process on_complete")

    async def start(self, workflow_instance: ITask, ignore_status=True) -> None:
        if ignore_status or self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            await self.execute(
                runtime_parameters=workflow_instance.runtime_parameters,
                workflow_instance=workflow_instance,
            )


class SystemTimerTask(SystemTask):
    async def execute(
        self, runtime_parameters: Dict[str, VT], workflow_instance: ITask = None
    ) -> None:
        start_time = time.time()
        try:
            await dagger.service.services.Dagger.app._store.execute_system_timer_task()  # type: ignore
        except Exception as ex:
            logger.warning(
                f"Exception in SystemTimerTask execute {ex} {traceback.format_stack()}"
            )

        end_time = time.time()
        logger.info(f"SystemTimerTask.execute took {end_time-start_time}")


class SensorTask(ITask[KT, VT], abc.ABC):
    match_only_one: bool = False

    async def start(self, workflow_instance: ITask) -> None:
        # pre-execute
        if self.status.code in [
            TaskStatusEnum.COMPLETED.name,
            TaskStatusEnum.SKIPPED.name,
        ]:
            return await self.on_complete(
                status=self.status, workflow_instance=workflow_instance
            )
        if self.status.code == TaskStatusEnum.NOT_STARTED.name:
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore

    async def _update_correletable_key(self, workflow_instance: ITask) -> None:
        """Updates the correletable key if the local is not the same as global key.

        Returns:
            None.
        """
        if workflow_instance and workflow_instance.runtime_parameters:
            global_key = workflow_instance.runtime_parameters.get(
                self.correlatable_key, None
            )
            if self.status.code in [
                TaskStatusEnum.NOT_STARTED.name,
                TaskStatusEnum.EXECUTING.name,
            ]:  # type: ignore
                await dagger.service.services.Dagger.app.update_correletable_key_for_task(  # type: ignore
                    self, str(global_key), workflow_instance=workflow_instance
                )  # type: ignore
        else:
            logger.error(
                f"Could not find root template instance. Did not update correlatable key for task with id: {str(self.get_id())}"
            )

    async def evaluate(self, **kwargs: Any) -> Optional[UUID]:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Executor does not evaluate")

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("Executor does not execute")


class IMonitoredTask:
    @abc.abstractmethod
    def get_monitoring_task_type(self) -> Type[MonitoringTask]:  # pragma: no cover
        ...

    @abc.abstractmethod
    async def setup_monitoring_task(
        self, workflow_instance: ITask
    ) -> None:  # pragma: no cover
        ...


class KafkaAgent:
    __topic: Topic
    __task: KafkaListenerTask

    def __init__(self, app: Service, topic: Topic, task: KafkaListenerTask) -> None:
        self.__topic = topic
        self.app = app
        self.__task = task

    async def process_event_helper(self, event):
        start_time = self.app.faust_app.loop.time()
        mappings = await self.__task.get_correlatable_keys_from_payload(event)
        processed_task = False
        if mappings:
            for mapping in mappings:
                if not mapping or len(mapping) < 2:
                    logger.warning(
                        f"Listener on topic {self.__topic.get_topic_name()} has incorrect mapping {mapping}"
                    )
                    continue
                updated_mapping = (
                    mapping[0],
                    f"{mapping[1]}_{self.__topic.get_topic_name()}",
                )
                async for workflow_instance, task_instance in self.app._get_tasks_by_correlatable_key(
                    updated_mapping, get_completed=True
                ):
                    try:
                        if task_instance and task_instance.topic:
                            if task_instance.topic == self.__topic.get_topic_name():
                                # Skip previous tasks if received task was in not started status.
                                if (
                                    task_instance.status.code
                                    == TaskStatusEnum.NOT_STARTED.name
                                    and task_instance.allow_skip_to  # noqa: W503
                                ):
                                    logger.debug(
                                        f"{task_instance} {event} is in a {TaskStatusEnum.NOT_STARTED.value} state. Previous task(s) will be skipped and this task will be set to {TaskStatusEnum.EXECUTING.value} status."
                                    )

                                    if workflow_instance is None:
                                        continue
                                    previous_tasks = (
                                        await workflow_instance.get_remaining_tasks(
                                            workflow_instance.root_dag,
                                            workflow_instance=workflow_instance,
                                            tasks=[],
                                            end_task_id=task_instance.get_id(),
                                        )
                                    )
                                    task_instance.status = TaskStatus(
                                        code=TaskStatusEnum.EXECUTING.name,
                                        value=TaskStatusEnum.EXECUTING.value,
                                    )
                                    task_instance.time_submitted = int(time.time())
                                    processed_task = True
                                    for task in previous_tasks[:-1]:
                                        if task.status.code in [
                                            TaskStatusEnum.NOT_STARTED.name,
                                            TaskStatusEnum.EXECUTING.name,
                                        ]:
                                            logger.debug(f"Skipped task {task} {event}")
                                            await task.on_complete(
                                                workflow_instance=workflow_instance,
                                                status=TaskStatus(
                                                    code=TaskStatusEnum.SKIPPED.name,
                                                    value=TaskStatusEnum.SKIPPED.value,
                                                ),
                                                iterate=False,
                                            )

                                if (
                                    task_instance.status.code
                                    == TaskStatusEnum.COMPLETED.name
                                ):
                                    if (
                                        hasattr(task_instance, "reprocess_on_message")
                                        and task_instance.reprocess_on_message
                                    ):
                                        await task_instance.on_message(
                                            workflow_instance.runtime_parameters, event
                                        )
                                        await workflow_instance._update_global_runtime_parameters()
                                    else:
                                        await task_instance.start(workflow_instance)
                                    processed_task = True
                                    continue

                                # Process on_message for task only if executing or skipped but set to allow out of order processing
                                elif (
                                    task_instance.status.code
                                    != TaskStatusEnum.EXECUTING.name
                                    and (
                                        task_instance.status.code
                                        != TaskStatusEnum.SKIPPED.name
                                        or not task_instance.allow_skip_to  # noqa: W503
                                    )
                                ):
                                    logger.info(
                                        f"Received event for task {task_instance} however the task was not in an executing state nor was it in a skipped state with out of order processing enabled. Not processing on_message for this task. Event: {event}"
                                    )
                                    continue
                                completed = await task_instance.on_message(
                                    workflow_instance.runtime_parameters, event
                                )
                                await workflow_instance._update_global_runtime_parameters()
                                if completed:
                                    await task_instance.on_complete(
                                        workflow_instance=workflow_instance
                                    )
                                processed_task = True

                                if getattr(self.__task, "match_only_one", False):
                                    logger.info(
                                        f"Matched exactly once on input topic {self.__topic.get_topic_name()}"
                                    )
                                    break
                    except Exception as ex:
                        logger.error(
                            f"Error processing event for {task_instance.id}. error {ex}",
                            exc_info=True,
                        )
        if not processed_task:
            logger.debug(
                f"listener agent on topic: {self.__topic} found no tasks for mapping"
            )
        end_time = dagger.service.services.Dagger.app.faust_app.loop.time() - start_time  # type: ignore
        if processed_task and getattr(dagger.service.services.Dagger.app, "dd_sensor", None):  # type: ignore
            dagger.service.services.Dagger.app.dd_sensor.client.histogram(  # type: ignore
                metric="process_event_helper", value=end_time
            )  # type: ignore

    async def process_event(self, stream):
        async for event in stream:
            await self.process_event_helper(event)

    def decorate(self, app: App, concurrency: int = 1) -> None:
        app.agent(self.__topic, name=self.__topic.get_topic_name(), concurrency=concurrency)(  # type: ignore
            self.process_event
        )  # type: ignore


class KafkaCommandTask(ExecutorTask[KT, VT], abc.ABC):
    topic: str = None


class KafkaListenerTask(SensorTask[KT, VT], abc.ABC):
    topic: str = None

    def get_correlatable_key(self, payload: Any) -> TaskLookupKey:
        return payload


class INonLeafNodeTask(ITask[KT, VT], abc.ABC):
    task_type: str = TaskType.SUB_DAG.name

    async def stop(self) -> None:
        pass

    async def start(self, workflow_instance: ITask) -> None:
        if self.status.code in [
            TaskStatusEnum.COMPLETED.name,
            TaskStatusEnum.SKIPPED.name,
        ]:
            return await self.on_complete(
                status=self.status, workflow_instance=workflow_instance
            )
        if (
            self.status.code == TaskStatusEnum.NOT_STARTED.name
            or self.status.code == TaskStatusEnum.SUBMITTED.name
        ):
            await self.execute(
                runtime_parameters=workflow_instance.runtime_parameters,
                workflow_instance=workflow_instance,
            )
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        logger.debug(
            f"Starting task {self.task_name} with root dag id {self.root_dag}, parent task id {self.parent_id}, and task id {self.id}"
        )
        first_dag_instance = workflow_instance.get_task(id=self.root_dag)
        if first_dag_instance:
            await first_dag_instance.start(workflow_instance=workflow_instance)
        else:
            logger.error(
                f"Could not find task instance for task with id: {self.root_dag}. Unable to start."
            )

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        logger.debug(f"starting execution of {self.id}")
        self.status = TaskStatus(
            code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
        )
        self.time_submitted = int(time.time())


class TaskOperator(Enum):
    ATLEAST_ONE = "ATLEAST_ONE"
    JOIN_ALL = "JOIN_ALL"


class ParallelCompositeTask(ITask[KT, VT], abc.ABC):
    """
    SUB-DAG Task to execute parallel tasks and wait until all of them are in a terminal state before progressing to the next task
    This task can be embedded as a child of the root node or a process node
    """

    task_type: str = TaskType.PARALLEL_COMPOSITE.name
    parallel_child_task_list: Set[UUID] = set()
    operator_type: str = TaskOperator.JOIN_ALL.name
    process_name: str = None

    async def stop(self) -> None:
        pass

    async def start(self, workflow_instance: ITask) -> None:
        await asyncio.sleep(0)
        if self.status.code in TERMINAL_STATUSES:
            return await self.on_complete(
                workflow_instance=workflow_instance, status=self.status
            )
        if (
            self.status.code == TaskStatusEnum.NOT_STARTED.name
            or self.status.code == TaskStatusEnum.SUBMITTED.name
        ):
            self.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.time_submitted = int(time.time())
            await self.execute(
                runtime_parameters=workflow_instance.runtime_parameters,
                workflow_instance=workflow_instance,
            )
            await dagger.service.services.Dagger.app._update_instance(task=workflow_instance)  # type: ignore
        logger.debug(
            f"Starting task {self.task_name} with parent task id {self.parent_id}, and task id {self.id}"
        )
        for task_id in self.parallel_child_task_list:
            dag_instance = workflow_instance.get_task(id=task_id)  # type: ignore
            if dag_instance:
                await dag_instance.start(workflow_instance)
            else:
                logger.error(
                    f"Could not find task instance for task with id: {task_id}. Unable to start."
                )

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        logger.debug(f"starting execution of ParallelCompositeTask {self.id}")
        self.status = TaskStatus(
            code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
        )
        self.time_submitted = int(time.time())

    async def notify(
        self, status: TaskStatus, workflow_instance: ITask = None
    ) -> None:  # pragma: no cover
        """If not completed, runs the steps required for completion by calling on_complete()."""
        atleast_one = False
        all_in_terminal = True
        if self.status.code != status.code:
            # check if all the child tasks are in terminal states
            for task_id in self.parallel_child_task_list:
                dag_instance: ITask = workflow_instance.get_task(task_id)  # type: ignore
                if dag_instance:
                    if dag_instance.status.code in TERMINAL_STATUSES:
                        atleast_one = True
                        if self.operator_type == TaskOperator.ATLEAST_ONE.name:
                            break
                    else:
                        all_in_terminal = False
                        if self.operator_type == TaskOperator.JOIN_ALL.name:
                            break
                else:
                    logger.error(
                        f"Could not find task instance for task with id: {task_id}. Unable to notify."
                    )
            if (
                self.operator_type == TaskOperator.JOIN_ALL.name and all_in_terminal
            ) or (self.operator_type == TaskOperator.ATLEAST_ONE.name and atleast_one):
                await self.on_complete(
                    workflow_instance=workflow_instance, status=status
                )


class IProcessTemplateDAGInstance(INonLeafNodeTask[KT, VT], abc.ABC):
    process_name: str = None
    max_run_duration_monitor_task_id: UUID = None
    max_run_duration: int = 0


class CorrelatableMapValue(Record):
    correlatable_key_attr: str
    correlatable_key_attr_value: str


class ITemplateDAGInstance(INonLeafNodeTask[KT, VT], abc.ABC):
    template_name: str = None
    partition_key_lookup: str = None
    task_type = TaskType.ROOT.name
    tasks: Dict[UUID, ITask] = {}
    sensor_tasks_to_correletable_map: Dict[UUID, CorrelatableMapValue] = {}
    runtime_parameters: Dict[str, VT] = None
    update_count: int = 0

    def add_task(self, task: ITask):
        self.tasks[task.id] = task

    def get_task(self, id: UUID) -> Optional[ITask]:
        if id == self.id:
            return self
        return self.tasks.get(id, None)

    async def _update_global_runtime_parameters(self) -> None:

        for (
            sensor_task_id,
            correletable_kv,
        ) in self.sensor_tasks_to_correletable_map.items():
            new_runtime_value: str = self.runtime_parameters.get(correletable_kv.correlatable_key_attr, None)  # type: ignore
            existing_value = correletable_kv.correlatable_key_attr_value
            sensor_task_instance = self.get_task(id=sensor_task_id)
            if (
                sensor_task_instance
                and sensor_task_instance.status.code
                in [TaskStatusEnum.NOT_STARTED.name, TaskStatusEnum.EXECUTING.name]
                and new_runtime_value != existing_value
            ):
                correletable_kv.correlatable_key_attr_value = new_runtime_value
                await sensor_task_instance._update_correletable_key(self)


class DefaultProcessTemplateDAGInstance(IProcessTemplateDAGInstance[str, str]):
    async def on_complete(
        self,
        workflow_instance: ITemplateDAGInstance,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        iterate: bool = True,
    ) -> None:
        if (
            hasattr(self, "max_run_duration_monitor_task_id")
            and self.max_run_duration_monitor_task_id
        ):
            max_run_duration_monitor_task: ITask = await dagger.service.services.Dagger.app.get_instance(  # type: ignore
                self.max_run_duration_monitor_task_id, log=False
            )
            if max_run_duration_monitor_task:
                await max_run_duration_monitor_task.on_complete(
                    iterate=iterate, workflow_instance=workflow_instance
                )
        await super().on_complete(
            workflow_instance=workflow_instance, status=status, iterate=iterate
        )

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        await super().execute(
            runtime_parameters=runtime_parameters, workflow_instance=workflow_instance
        )
        await self.setup_max_run_duration(wokflow_instance=workflow_instance)

    async def setup_max_run_duration(self, wokflow_instance: ITask) -> None:
        if hasattr(self, "max_run_duration") and self.max_run_duration != 0:
            max_run_duration_monitor_task: ITask = SkipOnMaxDurationTask(
                id=uuid.uuid1(),
                monitored_task_id=self.id,
                time_to_execute=int(time.time()) + self.max_run_duration,
            )
            wokflow_instance.add_task(task=max_run_duration_monitor_task)
            max_run_duration_monitor_task.status = TaskStatus(
                code=TaskStatusEnum.EXECUTING.name, value=TaskStatusEnum.EXECUTING.value
            )
            self.max_run_duration_monitor_task_id = max_run_duration_monitor_task.id

            await dagger.service.services.Dagger.app._store_trigger_instance(task_instance=max_run_duration_monitor_task, wokflow_instance=wokflow_instance)  # type: ignore

    async def on_message(
        self, runtime_parameters: Dict[str, VT], *args: Any, **kwargs: Any
    ) -> bool:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("ProcessInstance does not process on_message")

    async def evaluate(self, **kwargs: Any) -> Optional[ITask]:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("ProcessInstance does not process on_message")

    def get_correlatable_key(self, payload: Any) -> TaskLookupKey:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError(
            "ProcessInstance does not process get_correlatable_key"
        )


class MonitoredProcessTemplateDAGInstance(
    DefaultProcessTemplateDAGInstance, IMonitoredTask
):
    monitoring_task_id: UUID = None

    async def on_complete(
        self,
        workflow_instance: ITask,
        status: TaskStatus = TaskStatus(
            code=TaskStatusEnum.COMPLETED.name, value=TaskStatusEnum.COMPLETED.value
        ),
        iterate: bool = True,
    ) -> None:
        """Sets the status of the ITask to completed and starts the next ITask if there is one."""
        if self.monitoring_task_id:
            monitoring_task: ITask = workflow_instance.get_task(
                id=self.monitoring_task_id
            )
            if monitoring_task:
                await monitoring_task.on_complete(
                    workflow_instance=workflow_instance, iterate=iterate
                )
        await super().on_complete(
            workflow_instance=workflow_instance, status=status, iterate=iterate
        )

    async def execute(
        self, runtime_parameters: Dict[str, str], workflow_instance: ITask = None
    ) -> None:
        await super().execute(
            runtime_parameters=runtime_parameters, workflow_instance=workflow_instance
        )
        await self.setup_monitoring_task(workflow_instance=workflow_instance)

    async def setup_monitoring_task(self, workflow_instance: ITask) -> None:
        wait_time = workflow_instance.runtime_parameters.get(COMPLETE_BY_KEY, None)
        if wait_time:
            if self.monitoring_task_id is None:
                monitoring_task: ITask = self.get_monitoring_task_type()(
                    id=uuid.uuid1(),
                    monitored_task_id=self.id,
                    time_to_execute=wait_time,
                )
                monitoring_task.status = TaskStatus(
                    code=TaskStatusEnum.EXECUTING.name,
                    value=TaskStatusEnum.EXECUTING.value,
                )
                workflow_instance.add_task(task=monitoring_task)
                self.monitoring_task_id = monitoring_task.id
                await dagger.service.services.Dagger.app._store_trigger_instance(task_instance=monitoring_task, workflow_instance=workflow_instance)  # type: ignore

        logger.info(f"set up wait time {wait_time} task id {self.monitoring_task_id}")


class DefaultTemplateDAGInstance(ITemplateDAGInstance[str, str]):
    def get_correlatable_key(self, payload: Any) -> TaskLookupKey:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError(
            "DefaultTemplateDAGInstance does not process get_correlatable_key"
        )

    async def evaluate(self, **kwargs: Any) -> Optional[ITask]:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError("DefaultTemplateDAGInstance does not evaluate")

    async def on_message(
        self, runtime_parameters: Dict[str, VT], *args: Any, **kwargs: Any
    ) -> bool:
        """Not implemented.

        Raises:
            NotImplementedError: Not implemented.
        """
        raise NotImplementedError(
            "DefaultTemplateDAGInstance does not process on_message"
        )


class Trigger(Record, serializer="raw"):  # type: ignore
    trigger_time: int = int(time.time())
    id: UUID = None
    workflow_id: UUID = None

    def get_trigger_key(self) -> Tuple[UUID, UUID]:
        return self.workflow_id, self.id


class CorreletableLookUpKey(Record, serializer="raw"):  # type: ignore
    workflow_id: UUID
    task_id: UUID


class CorreletableKeyTasks(Record, serializer="raw"):  # type: ignore
    lookup_keys: Set[CorreletableLookUpKey] = set()
    overflow_key: Optional[str] = None
    key: str = None
