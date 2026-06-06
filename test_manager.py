import manager
import sys

def run_tests():
    tests = [
        "애플 현재가만 알려줘",
        "삼성전자 지금 사도 될까?",
        "삼성전자랑 SK하이닉스 비교해줘"
    ]
    for idx, t in enumerate(tests, 1):
        print(f"\n{'='*60}")
        print(f" [Test {idx}] {t}")
        print(f"{'='*60}")
        try:
            ans = manager.manager(t)
            print("\n>>> 결과:\n" + ans)
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    run_tests()
