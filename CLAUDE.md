# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# 프로젝트
다운로드 폴더에 새 PDF가 생기면 자동으로 감지해 논문을 주제별로 분류하고, 검색 가능한 아카이브로 정리하는 시스템.

## 실행 방법
분류 로직은 `classify.py <파일경로>`로 호출된다. 사람이 직접 실행하지 않고, 윈도우 쪽 PowerShell FileSystemWatcher가 다운로드 폴더에서 새 PDF를 감지하면 `wsl -d Ubuntu -- bash -c "..."`로 이 스크립트를 호출하는 구조다.

## 세션 시작 시
STATE.md의 「지금」 섹션을 먼저 확인한다.

## 완료 보고
작업 완료를 보고할 때는 실행한 명령과 그 출력(또는 테스트 결과)을 증거로 같이 제시한다.

## 리서치 자료
리서치·조사·외부 문서 정리는 전부 docs/에 파일로 저장한다. docs/는 자동으로 읽지 않으며, 파일명을 지목할 때만 읽는다.

## 검증
기능 작업 완료 전 반드시 다음을 통과시킨다: python3 -m py_compile classify.py
