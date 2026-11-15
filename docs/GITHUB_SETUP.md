# Publish this project to a personal private GitHub repository

Suggested repository name: `frontier-model-register`.

This project includes the Python source, documentation, offline tests, public-source evidence datasets, original initialization workbook, and populated Excel/HTML examples. The `.gitignore` excludes operational state, generated runtime exports, local configuration, review files, decision files, and credential files. The source snapshots retain their attribution.

The instructions below create a new private repository. Use your personal account for authentication. They do not change the visibility of an existing repository or overwrite one.

## GitHub CLI route

Install Git and GitHub CLI on your computer if needed, then open a terminal in the extracted `frontier-model-register` directory. In PowerShell, the commands below can be run one line at a time.

```text
gh auth login --hostname github.com --git-protocol https --web
gh auth setup-git
gh api user --jq .login
```

The last command displays the authenticated account. Confirm it is your personal account before creating the repository. Complete authentication through GitHub's browser flow.

Initialize the local repository and inspect its contents:

```text
git init -b main
git status --short
git add .
git diff --cached --stat
```

If Git does not already have your preferred commit identity, set it locally for this repository using your chosen author name and your GitHub-verified or GitHub-provided private email address:

```text
git config user.name "YOUR COMMIT AUTHOR NAME"
git config user.email "YOUR GITHUB COMMIT EMAIL"
```

Create the initial commit:

```text
git commit -m "Initial import of frontier model register v1.1.0"
```

Create the repository as private, inspect its returned visibility, then push:

```text
gh repo create frontier-model-register --private --source=. --remote=origin --description "Frontier model evidence register, cyber benchmark monitoring, Excel exports and Confluence publication"
gh repo view --json nameWithOwner,isPrivate,url
```

Proceed when the owner is your personal account and `isPrivate` is `true`:

```text
git push -u origin main
gh repo view --web
```

If the repository name already exists, stop and inspect it before choosing a different name. Do not force-push or change an existing repository's visibility to work around the conflict.

## Find the populated workbook

Open `examples/first_run_model_register.xlsx`. The **Benchmark Results** worksheet contains the published benchmark observations; **AISI Evidence** contains the separate cyber-range findings. `data/Bank_Frontier_Model_Register_2026-11-14.xlsx` is the original input workbook.

## Keep operational files outside version control

After installation, copy `config.example.yaml` to `config.yaml` and initialize a local `work/` directory as described in the main README. These local files are ignored. Before future commits, use `git status --short` and review the staged changes.

The repository contains no automatically running GitHub Actions workflow or live Confluence configuration. The existing Azure Pipelines file is an example. Configure execution and destinations separately when needed.

## Official references

- [Create a repository with GitHub CLI](https://cli.github.com/manual/gh_repo_create)
- [GitHub CLI browser authentication](https://cli.github.com/manual/gh_auth_login)
- [GitHub CLI repository details](https://cli.github.com/manual/gh_repo_view)
- [Create a GitHub repository](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)
