from __future__ import annotations

from fastapi import APIRouter, Query

from data_threads import run_data_task
from services import concepts


router = APIRouter()


CONCEPT_ALIAS_RESOLVE_DESCRIPTION = """`GET` 根据外部 provider 的题材板块标识，解析对应的系统 Concept ID。

这个接口用于调用方已经知道某个外部板块，例如 `tushare / ths / 885959`，
想知道它在 MarketHub 内部被归并到哪个系统概念。

## 查询参数

- `provider`（`str`）：外部数据源 ID，例如 `tushare`、`akshare`。
- `board_type`（`str`）：provider 内部题材库类型，例如 `ths`、`dc`、`tdx`、`kpl`。为空时只按 `provider + board_code` 查找。
- `board_code`（`str`）：外部板块代码。
- `trade_date`（`str`）：可选查询日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`。填写后，只有该日期仍有效的成员关系才会被用于解析；为空时使用完整概念超集。

## 返回类型

顶层返回 `ConceptAliasResolveItem`。

## 返回字段

- `concept_id`（`str`）：系统 Concept ID，例如 `C71`。没有匹配结果时返回空字符串。
- `canonical_name`（`str`）：系统概念规范名称，例如 `PCB`。没有匹配结果时返回空字符串。
- `confidence`（`float | None`）：最终映射置信度。当前读取的是已确认映射，匹配时返回 `1.0`；没有匹配结果时返回 `null`。"""


CONCEPT_ALIAS_GROUPS_DESCRIPTION = """`GET` 列出系统 Concept ID 与外部 provider 题材板块的对应关系表。

这个接口返回当前 Concept Alias 资产中的全部系统概念。Concept Alias 是超集：
即使某个题材只存在于一个 provider、无法和其他 provider 关联，也会拥有自己的系统 `concept_id`。

## 查询参数

- `trade_date`（`str`）：可选查询日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`。为空时返回完整超集和完整成员列表；填写后只保留该日期有效的成员。

## 返回类型

顶层返回 `list[ConceptAliasGroupItem]`。

## 返回字段

- `concept_id`（`str`）：系统 Concept ID，格式为 `C1`、`C2`、`C123`。
- `canonical_name`（`str`）：系统概念规范名称。
- `start_date`（`str`）：系统概念起始日期，来自所有成员板块的最早起始日期；缺失起始日期统一按 `2000-01-01` 处理。
- `end_date`（`str`）：系统概念结束日期。为空表示尚未结束或结束日期未知。
- `members`（`list[ConceptAliasGroupMemberItem]`）：该系统概念下的外部 provider 板块列表。
- `members[].provider`（`str`）：外部数据源 ID。
- `members[].board_type`（`str`）：provider 内部题材库类型。
- `members[].board_code`（`str`）：外部板块代码。
- `members[].board_name`（`str`）：外部板块名称。
- `members[].start_date`（`str`）：该外部板块关系起始日期。
- `members[].end_date`（`str`）：该外部板块关系结束日期。为空表示尚未结束或结束日期未知。"""


CONCEPT_ALIAS_GROUP_DESCRIPTION = """`GET` 查询单个系统 Concept ID 下的 provider 题材板块成员。

这个接口用于调用方已经拿到系统 `concept_id`，需要查看它当前关联了哪些外部题材板块。
如果指定 `trade_date`，返回的 `members` 会按成员自身的 `start_date/end_date` 过滤。

## 路径参数

- `concept_id`（`str`）：系统 Concept ID，例如 `C71`。

## 查询参数

- `trade_date`（`str`）：可选查询日期，支持 `YYYYMMDD` 或 `YYYY-MM-DD`。为空时返回该概念的完整成员；填写后只返回该日期有效的成员。

## 返回类型

顶层返回 `ConceptAliasGroupItem`。

## 返回字段

- `concept_id`（`str`）：系统 Concept ID。找不到时返回空字符串。
- `canonical_name`（`str`）：系统概念规范名称。找不到时返回空字符串。
- `start_date`（`str`）：系统概念起始日期，来自完整成员范围，不因 `trade_date` 过滤而改变。
- `end_date`（`str`）：系统概念结束日期。为空表示尚未结束或结束日期未知。
- `members`（`list[ConceptAliasGroupMemberItem]`）：该系统概念下的外部 provider 板块列表；指定 `trade_date` 时只包含该日期有效的成员。"""


def _dump_item(loader, args: tuple[object, ...]) -> dict[str, object]:
    return loader(*args).model_dump()


def _dump_item_list(loader, args: tuple[object, ...]) -> list[dict[str, object]]:
    return [item.model_dump() for item in loader(*args)]


@router.get("/api/concepts/alias/resolve", summary="解析外部题材板块对应的系统 Concept ID", description=CONCEPT_ALIAS_RESOLVE_DESCRIPTION)
async def api_concept_alias_resolve(
    provider: str = Query(..., description="外部数据源 ID，例如 tushare、akshare。"),
    board_type: str = Query("", description="同一 provider 内部的题材库类型，例如 ths、dc、tdx、kpl。为空时只按 provider 和 board_code 匹配。"),
    board_code: str = Query(..., description="外部 provider 的题材/概念板块代码，例如 885959、BK0877、308832。"),
    trade_date: str = Query("", description="查询日期，支持 YYYYMMDD 或 YYYY-MM-DD。为空时查询完整超集；填写后只返回该日期有效的映射。"),
) -> dict[str, object]:
    """
    根据外部 provider 的题材板块标识，解析对应的系统 Concept ID。

    这个接口用于调用方已经知道某个外部板块，例如 `tushare / ths / 885959`，
    想知道它在 MarketHub 内部被归并到哪个系统概念。

    参数说明：
    - `provider`：外部数据源 ID，例如 `tushare`、`akshare`。
    - `board_type`：provider 内部题材库类型，例如 `ths`、`dc`、`tdx`、`kpl`。为空时只按 `provider + board_code` 查找。
    - `board_code`：外部板块代码。
    - `trade_date`：可选查询日期。填写后，只有该日期仍有效的成员关系才会被用于解析；为空时使用完整概念超集。

    返回值说明：
    - `concept_id`：系统 Concept ID，例如 `C71`。没有匹配结果时返回空字符串。
    - `canonical_name`：系统概念规范名称，例如 `PCB`。没有匹配结果时返回空字符串。
    - `confidence`：最终映射置信度。当前读取的是已确认映射，匹配时返回 `1.0`；没有匹配结果时返回 `null`。
    """
    return await run_data_task(_dump_item, concepts.resolve_alias, (provider, board_type, board_code, trade_date))


@router.get("/api/concepts/alias/groups", summary="列出系统概念与外部题材板块对应关系", description=CONCEPT_ALIAS_GROUPS_DESCRIPTION)
async def api_concept_alias_groups(
    trade_date: str = Query("", description="查询日期，支持 YYYYMMDD 或 YYYY-MM-DD。为空时返回完整概念超集；填写后每个概念只保留该日期有效的成员。"),
) -> list[dict[str, object]]:
    """
    列出系统 Concept ID 与外部 provider 题材板块的对应关系表。

    这个接口返回当前 Concept Alias 资产中的全部系统概念。Concept Alias 是超集：
    即使某个题材只存在于一个 provider、无法和其他 provider 关联，也会拥有自己的系统 `concept_id`。

    参数说明：
    - `trade_date`：可选查询日期。为空时返回完整超集和完整成员列表；填写后只保留该日期有效的成员。

    返回值说明：
    - 返回数组，每个元素是一组系统概念。
    - `concept_id`：系统 Concept ID，格式为 `C1`、`C2`、`C123`。
    - `canonical_name`：系统概念规范名称。
    - `start_date`：系统概念起始日期，来自所有成员板块的最早起始日期；缺失起始日期统一按 `2000-01-01` 处理。
    - `end_date`：系统概念结束日期。为空表示尚未结束或结束日期未知。
    - `members`：该系统概念下的外部 provider 板块列表。
    - `members[].provider`：外部数据源 ID。
    - `members[].board_type`：provider 内部题材库类型。
    - `members[].board_code`：外部板块代码。
    - `members[].board_name`：外部板块名称。
    - `members[].start_date`：该外部板块关系起始日期。
    - `members[].end_date`：该外部板块关系结束日期。为空表示尚未结束或结束日期未知。
    """
    return await run_data_task(_dump_item_list, concepts.list_alias_groups, (trade_date,))


@router.get("/api/concepts/alias/groups/{concept_id}", summary="查询单个系统概念的外部题材板块成员", description=CONCEPT_ALIAS_GROUP_DESCRIPTION)
async def api_concept_alias_group(
    concept_id: str,
    trade_date: str = Query("", description="查询日期，支持 YYYYMMDD 或 YYYY-MM-DD。为空时返回该 concept 的完整成员；填写后只保留该日期有效的成员。"),
) -> dict[str, object]:
    """
    查询单个系统 Concept ID 下的 provider 题材板块成员。

    这个接口用于调用方已经拿到系统 `concept_id`，需要查看它当前关联了哪些外部题材板块。
    如果指定 `trade_date`，返回的 `members` 会按成员自身的 `start_date/end_date` 过滤。

    参数说明：
    - `concept_id`：系统 Concept ID，例如 `C71`。
    - `trade_date`：可选查询日期。为空时返回该概念的完整成员；填写后只返回该日期有效的成员。

    返回值说明：
    - `concept_id`：系统 Concept ID。找不到时返回空字符串。
    - `canonical_name`：系统概念规范名称。找不到时返回空字符串。
    - `start_date`：系统概念起始日期，来自完整成员范围，不因 `trade_date` 过滤而改变。
    - `end_date`：系统概念结束日期。为空表示尚未结束或结束日期未知。
    - `members`：该系统概念下的外部 provider 板块列表；指定 `trade_date` 时只包含该日期有效的成员。
    """
    return await run_data_task(_dump_item, concepts.get_alias_group, (concept_id, trade_date))
