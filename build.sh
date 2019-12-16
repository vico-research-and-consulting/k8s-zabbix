#!/bin/bash

####################################################################
## Helpers

notice(){
   echo -e "\e[1;32m$1\e[0m"
}

# Parameter:
#   1: cmd
# Execute simple shell command, exit if errorcode of shell command != 0
exec_cmd(){
   local CMD="$1"
   echo "+ $CMD"
   eval "$CMD 2>&1"
   local RET="$?"
   if [ "$RET" != "0" ];then
      echo "ERROR: execution failed (returncode $RET)"
      exit 2
   fi
   return 0
}


####################################################################
## MAIN

VERSION="${VERSION:-$(git describe --abbrev=0 --tags)}"
TIMESTAMP="$(date --date="today" "+%Y%m%d%H%M%S")"

DOCKER_REPO_URL_RELEASE="repo.vico-research.com:15000/repository/docker-release/sre"
DOCKER_REPO_URL_SNAPSHOT="repo.vico-research.com:15001/repository/docker-snapshot/sre"

DOCKER_SQUASH="${DOCKER_SQUASH:-true}"


DELAY="35"

BDIR="$(dirname $(readlink -f $0))"
cd $BDIR || exit 1

# PHASES
build_image(){
   if [ -z "$VERSION" ];then
      echo "ERROR: no git release tag available"
      exit 1
   fi
   echo "$VERSION/$TIMESTAMP" > scripts/release-info
   if [ "$DOCKER_SQUASH" == "true" ];then
      SQUASH_OPT="--squash"
      notice "Squashing of image is enabled, you can disable that by 'export DOCKER_SQUASH=false'"
   else
      SQUASH_OPT=""
   fi
   exec_cmd "docker build $SQUASH_OPT -t ${IMAGE_BASE} -f Dockerfile ."
   SIZE="$(docker inspect $IMAGE_BASE --format='{{.Size}}')"
   notice "Image size $(( $SIZE / 1024 / 1024 ))MB"
}

test_container(){
   IDENT="${IMAGE_NAME}_test"
   docker kill $IDENT
   docker rm $IDENT
   exec_cmd "docker run --rm --env ZABBIX_SERVER='localhost' --env ZABBIX_HOST='localhost' -d --name $IDENT ${IMAGE_BASE} config_default"
   sleep 10
   echo "====== DOCKER LOGS"
   docker logs --until=50s $IDENT
   echo "=================="
   exec_cmd "docker ps |grep $IDENT"
   exec_cmd "docker kill $IDENT"
   #exec_cmd "grep -q -P ': Tomcat started on port\(s\): 8080 \(http\) with context path' /tmp/$IDENT/javabase-test/spring.log"
   #exec_cmd "grep -q -P ': Started Application in .* seconds' /tmp/$IDENT/javabase-test/spring.log"
   #exec_cmd "grep -q -P 'Using G1' /tmp/$IDENT/javabase-test/the_container_name_*"
}


inspect(){
   IDENT="${IMAGE_NAME}_test"
   exec_cmd "docker run -ti --rm --env ZABBIX_SERVER='localhost' --env ZABBIX_HOST='localhost' --name $IDENT ${IMAGE_BASE} /bin/bash"
}


cleanup(){
  exec_cmd "rm -rf /tmp/${IMAGE_NAME}*"
  exec_cmd "docker rmi ${IMAGE_NAME} --force"
  exec_cmd "docker rmi ${IMAGE_NAME}-test --force"
}


publish_image(){
  TIMESTAMP="$(date --date="today" "+%Y-%m-%d_%H-%M-%S")"

  exec_cmd "docker tag ${IMAGE_NAME}:${VERSION} $DOCKER_REPO_URL_SNAPSHOT/${IMAGE_NAME}:latest"
  exec_cmd "docker push $DOCKER_REPO_URL_SNAPSHOT/${IMAGE_NAME}:latest"

  exec_cmd "docker tag ${IMAGE_NAME}:${VERSION} $DOCKER_REPO_URL_RELEASE/${IMAGE_NAME}:${VERSION}.${TIMESTAMP}"
  exec_cmd "docker push $DOCKER_REPO_URL_RELEASE/${IMAGE_NAME}:${VERSION}.${TIMESTAMP}"
}

DEFAULT_PHASES="cleanup build_image test_container"
if [ -z "$1" ];then
  notice "CMD:" 
  echo
  echo "$0 <phase>...<phase>"
  echo
  notice "AVAILABLE PHASES:"
  echo  " -  default"
  echo  "    ($DEFAULT_PHASES)"
  echo  " -  inspect"
  for PHASE in $DEFAULT_PHASES; do
    echo " -  $PHASE"
  done
  echo " -  publish_image (optional)"
  echo " -  cleanup_openjdk (optional)"
  exit 0
fi

PHASES=""
for PHASE in $@
do
  if [ "$PHASE" = "default" ];then
     PHASES="$PHASES $DEFAULT_PHASES"
  else
     PHASES="$PHASES $PHASE"
  fi
done

IMAGE_NAME="kubernetes-zabbix"
IMAGE_BASE="${IMAGE_NAME}:${VERSION}"

for PHASE in $PHASES;
do
   if ( type "$PHASE" >/dev/null 2>&1 );then
      notice "INFO: PHASE $PHASE for $IMAGE_BASE"
      $PHASE
   else
      notice "ERROR: no such phase : $PHASE"
      exit 1
   fi
done

#SIZE="$(docker inspect $IMAGE_BASE --format='{{.Size}}')"
#notice "Image size $(( $SIZE / 1024 / 1024 ))MB"
notice "SUCESSFULLY COMPLETED"
