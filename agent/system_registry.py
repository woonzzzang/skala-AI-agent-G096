# LLm이 사용자 문장을 보고 어떤 시스템인지 추론하면
# Registry는 LLM이 고른 시스템이 실제로 존재하는지 확인하고 확정하는 역할 
# -> LLM에게 어떤 시스템인지 추론할 자유는 주지만, 실제 존재하는 시스템을 확정할 권한은 코드에 둠

"""
[구조]

monitoring.json 로드
    ↓
시스템명 & 설명을 LLM에게 전달
    ↓
LLM이 key 선택
    ↓
SystemRegistry가 실제 key인지 검증
    ↓
검증된 key로 Monitoring 조회
    ↓
해당 key의 status, CPU, memory, alerts 조회
    ↓
resolved 또는 interrupt
"""

"""monitoring.json 기반 시스템 목록과 시스템명 검증"""

import json
from pathlib import Path
from typing import Any

DEFAULT_MONITORING_PATH = (
    Path(__file__).resolve().parent.parent
    / "data"
    / "monitoring.json"
)

class SystemRegistryError(RuntimeError):
    """monitoring.json을 읽거나 검증할 수 없을 때"""

class SystemRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_MONITORING_PATH
        self._systems = self._load()

        # LLM이 대소문자를 다르게 출력해도 정식 이름으로 복원하기 위한 인덱스
        self._name_index = {
            name.casefold() : name
            for name in self._systems
        }

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(
                self.path.read_text(encoding="utf-8")
            )
        except OSError as exc:
            raise SystemRegistryError(
                f"monitoring.json을 읽을 수 없습니다: {self.path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise SystemRegistryError(
                f"monitoring.json 형식이 올바르지 않습니다: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise SystemRegistryError(
                "monitoring.json의 최상위 값은 객체여야 합니다."
            )

        for system_name, info in data.items():
            if not isinstance(system_name, str):
                raise SystemRegistryError(
                    "시스템은 문자열이어야 합니다."
                )

            if not isinstance(info, dict):
                raise SystemRegistryError(
                    f"{system_name}의 값은 객체여야 합니다."
                )

            # aliases = info.get("aliases", [])
            description = info.get("description", "")

            # if not isinstance(aliases, list):
            #     raise SystemRegistryError(
            #         f"{system_name}.aliases는 배열이어야 합니다."
            #     )

            # if not all(isinstance(alias,str) for alias in aliases):
            #     raise SystemRegistryError(
            #         f"{system_name}.aliases에는 문자열만 사용할 수 있습니다."
            #     )

            if not isinstance(description, str):
                raise SystemRegistryError(
                    f"{system_name}.description은 문자열이어야 합니다."
                )

        return data

    def names(self) -> list[str]:
        """등록된 시스템명 반환"""
        return list(self._systems.keys())

    def to_prompt_catalog(self) -> list[dict[str, Any]]:
        """Target Resolver LLM에 전달할 정보만 반환"""
        return [
            {
                "name": system_name,
                "description": info.get("description", ""),
            }
            for system_name, info in self._systems.items()
        ]

    def canonicalize(self, candidate: str) -> str | None:
        """LLM이 반환한 이름을 실제 시스템명으로 변환"""
        if not isinstance(candidate, str):
            return None

        normalized = candidate.strip().casefold()
        return self._name_index.get(normalized)

    def validate_candidates(
            self,
            candidates: list[str],
    ) -> list[str]:
        """LLM 후보 중 monitoring.json에 존재하는 시스템만 반환"""
        validated: list[str] = []

        for candidate in candidates:
            system_name = self.canonicalize(candidate)

            if (
                system_name is not None
                and system_name not in validated
            ):
                validated.append(system_name)

        return validated

    def get(self, system_name: str) -> dict[str, Any] | None:
        """정식 시스템명에 해당하는 monitoring 데이터를 반환"""
        canonical_name = self.canonicalize(system_name)

        if canonical_name is None:
            return None

        return self._systems[canonical_name]
