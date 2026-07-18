// MyNestra production deploy pipeline.
// Runs in the per-app deploy dir /var/jenkins_home/deployments/mynestra so `docker compose`
// auto-loads the gitignored `.env` (secrets) that lives there. The Jenkins container has the
// docker CLI + compose plugin and the host docker.sock. Migrations run via the image entrypoint
// on container start (waits for db -> migrate_schemas --shared -> ensure_public_tenant).
//
// Job setup: a Pipeline job with "Pipeline script from SCM" -> Git
// https://github.com/sajisukumaran/mynestra (branch main), Script Path "Jenkinsfile".
// Do NOT enable "wipe out workspace / clean before checkout" — it would delete the deploy `.env`.
pipeline {
    agent {
        node {
            label ''
            customWorkspace '/var/jenkins_home/deployments/mynestra'
        }
    }

    options {
        timestamps()
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    // Poll GitHub every ~2 min and build when main moves. Matches the teams/lhive
    // pattern; the edge Jenkins isn't publicly reachable, so a push webhook can't
    // work — polling is how all the dockerlab jobs self-trigger.
    triggers {
        pollSCM('H/2 * * * *')
    }

    environment {
        COMPOSE = 'docker compose -f compose.prod.yaml'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }
        stage('Build') {
            steps {
                sh "${COMPOSE} build"
            }
        }
        stage('Deploy') {
            steps {
                // --remove-orphans clears containers left by a since-renamed service (e.g. the old
                // `db` service, whose container was named `mynestra-db` — the same name the renamed
                // `mynestra-db` service now claims, which would otherwise collide). Named volumes
                // (mynestra_pgdata, mynestra_media_files) are project-scoped and survive this.
                sh "${COMPOSE} up -d --remove-orphans"
            }
        }
        stage('Prune') {
            steps {
                sh 'docker image prune -f'
            }
        }
        stage('Verify') {
            steps {
                sh "${COMPOSE} ps"
                // The entrypoint runs migrations + ensure_public_tenant on start, so poll /health/
                // (200 once the DB is reachable) from inside the web container.
                sh '''
                  for i in $(seq 1 30); do
                    if docker compose -f compose.prod.yaml exec -T web curl -fsS http://localhost:8000/health/ >/dev/null 2>&1; then
                      echo "health OK"; exit 0
                    fi
                    echo "waiting for web to become healthy ($i/30)..."; sleep 5
                  done
                  echo "web did not become healthy in time"
                  docker compose -f compose.prod.yaml logs --tail=80 web
                  exit 1
                '''
            }
        }
    }

    post {
        failure {
            sh "${COMPOSE} logs --tail=120 || true"
        }
    }
}
