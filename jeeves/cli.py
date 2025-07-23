# jeeves/cli.py

import sys
import pathlib

import click
import importlib
import pkgutil

PIPELINE_PKG = "jeeves.pipelines"

def discover_pipelines():
    """
    Dynamically import all modules under jeeves.pipelines
    and return a dict name -> module for those that define run().
    """
    import jeeves.pipelines as ppkg
    pipelines = {}
    for _, name, ispkg in pkgutil.iter_modules(ppkg.__path__):
        if ispkg:
            continue
        module = importlib.import_module(f"{PIPELINE_PKG}.{name}")
        if hasattr(module, "run") and callable(module.run):
            pipelines[name] = module
    return pipelines

@click.group()
def cli():
    """Jeeves — Rocket.Chat provisioning butler."""
    pass

@cli.group()
def pipelines():
    """Manage pipelines."""
    pass

@pipelines.command("list")
def list_pipelines():
    """List all available pipelines."""
    pipelines = discover_pipelines()
    click.echo("Available pipelines:")
    for name in sorted(pipelines):
        click.echo(f"  - {name}")

@pipelines.command("run", context_settings=dict(
    ignore_unknown_options=True,
    allow_extra_args=True
))
@click.argument("pipeline_name")
@click.pass_context
def run_pipeline(ctx, pipeline_name):
    """
    Run a pipeline. Pass any --key value options after the pipeline name.

    e.g.
      jeeves pipelines run ec2_setup --stack-name foo --instance-type t3.small
    """
    pipelines = discover_pipelines()
    if pipeline_name not in pipelines:
        click.echo(f"Error: pipeline '{pipeline_name}' not found.")
        ctx.exit(1)

    module = pipelines[pipeline_name]
    run_fn = module.run

    # parse out --key value pairs from ctx.args
    args = ctx.args
    it = iter(args)
    kwargs = {}
    for token in it:
        if token.startswith("--"):
            key = token.lstrip("-").replace("-", "_")
            try:
                val = next(it)
            except StopIteration:
                click.echo(f"Error: expected a value after '{token}'")
                ctx.exit(1)
            kwargs[key] = val
        else:
            click.echo(f"Ignoring unexpected token: {token}")

    try:
        run_fn(**kwargs)
    except TypeError as te:
        click.echo(f"Argument error: {te}")
        ctx.exit(1)
    except Exception as e:
        click.echo(f"Pipeline '{pipeline_name}' failed: {e}")
        ctx.exit(1)

@cli.group()
def describe():
    """Describe pipelines (print their markdown docs)."""
    pass

@describe.command("pipeline")
@click.argument("pipeline_name")
def describe_pipeline(pipeline_name):
    """
    Print the Markdown docs for a given pipeline.
    """
    pipelines = discover_pipelines()
    module = pipelines.get(pipeline_name)
    if not module:
        click.echo(f"Error: pipeline '{pipeline_name}' not found.")
        sys.exit(1)

    # 1) Try module‐level docs_path
    docs = getattr(module, "docs_path", None)

    # 2) Fallback: look for a Pipeline subclass with docs_path
    if not docs:
        from jeeves.pipeline import Pipeline  # absolute import
        for obj in module.__dict__.values():
            if isinstance(obj, type) and issubclass(obj, Pipeline) and hasattr(obj, "docs_path"):
                docs = obj.docs_path
                break

    # 3) Turn to Path and check
    if docs:
        docs_file = pathlib.Path(docs)
        if not docs_file.is_file():
            # fallback to module's own docs/ subfolder
            alt = pathlib.Path(module.__file__).parent / "docs" / f"{pipeline_name}.md"
            if alt.is_file():
                docs_file = alt
            else:
                docs_file = None
    else:
        docs_file = None

    if not docs_file:
        click.echo(f"No documentation available for '{pipeline_name}'.")
        sys.exit(1)

    click.echo(docs_file.read_text())

def main():
    cli()

if __name__ == "__main__":
    main()
