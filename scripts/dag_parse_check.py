"""用**真 Airflow** 的 DagBag 解析一遍生成的 DAG。只依赖 airflow，故可在 Airflow 镜像里跑。

为什么不在后端的单元测试里做：Airflow 2.10 不支持仓库后端所用的 Python 版本，装不进同一个
venv；而「能不能被导入」这件事必须由**实际会解析它的那个 Airflow 版本 + provider 集合**
来回答，装一个凑合的版本反而给假绿灯。故把这一步放进镜像，用 ``make dag-parse`` 触发。

退出码：0 全部导入成功；1 有 import error 或有 DAG 没有任何任务。
"""

from __future__ import annotations

import sys


def main(dags_folder: str) -> int:
    from airflow.models import DagBag

    bag = DagBag(dag_folder=dags_folder, include_examples=False, read_dags_from_db=False)

    if bag.import_errors:
        print(f"\n✗ {len(bag.import_errors)} 个文件导入失败：\n")
        for path, err in bag.import_errors.items():
            print(f"--- {path}")
            print(err.rstrip() if isinstance(err, str) else err)
            print()
        return 1

    if not bag.dags:
        print(f"✗ {dags_folder} 里没有解析出任何 DAG（目录空？路径写错？）")
        return 1

    problems: list[str] = []
    print(f"\n✓ {len(bag.dags)} 个 DAG 全部导入成功：\n")
    for dag_id in sorted(bag.dags):
        dag = bag.dags[dag_id]
        # **根任务必须只有 create_tables 一个**：搬运任务在建表之前跑起来会写进一张
        # 还不存在的表。层间连线断掉时任务数不变、导入也不报错，只有根任务会从 1 个
        # 变成一堆——这是这条检查里唯一能抓到「连线悄悄断了」的信号。
        roots = sorted(t.task_id for t in dag.tasks if not t.upstream_task_ids)
        print(f"  {dag_id}: {len(dag.tasks)} 个任务，根任务 {roots}")
        if not dag.tasks:
            problems.append(f"{dag_id}: 一个任务都没有")
        elif roots != ["create_tables"]:
            problems.append(
                f"{dag_id}: 根任务应只有 create_tables，实际 {roots}"
                "（这些任务会在建表之前跑）"
            )

    if problems:
        print("\n✗ 结构有问题：")
        for p in problems:
            print(f"  - {p}")
        return 1
    return 0


if __name__ == "__main__":
    folder = sys.argv[1] if len(sys.argv) > 1 else "/opt/airflow/dags"
    sys.exit(main(folder))
