"""Ch05 full acceptance collector; results need explicit semantic review."""
from scripts.evaluate_workflow import main as evaluate_main


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url',default='http://127.0.0.1:18805');parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    return evaluate_main(['run','--base-url',args.base_url,'--output',args.output])


if __name__=='__main__':raise SystemExit(main())
